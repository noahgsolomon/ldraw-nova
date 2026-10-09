# SPDX-License-Identifier: AGPL-3.0-only
import base64
from concurrent.futures import ThreadPoolExecutor
import dataclasses
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import socket
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock

import server


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2e0AAAAASUVORK5CYII=')


def archive(entries):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w') as out:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                info.linkname = '/etc/passwd'
            elif kind == tarfile.REGTYPE:
                info.size = len(content)
            out.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


class FakeRunner:
    def __init__(self):
        self.count = 0
        self.lock = threading.Lock()
        self.entered = threading.Event()
        self.gate = None

    def run(self, workspace, body, cancelled):
        with self.lock:
            self.count += 1
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        staged = Path(tempfile.mkdtemp(prefix='generation-', dir=workspace.parent))
        shutil.copytree(workspace, staged, dirs_exist_ok=True)
        (staged / 'model.mpd').write_text('0 Test\n0 STEP\n')
        (staged / 'preview.png').write_bytes(PNG)
        return 0, 'Built actual files', staged

    def close(self):
        pass


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.toolkit = root / 'toolkit'
        self.toolkit.mkdir()
        (self.toolkit / 'docs').mkdir()
        (self.toolkit / 'docs' / 'guide.md').write_text('A real public guide')
        (self.toolkit / 'instructions.md').write_text('Use full toolkit')
        (self.toolkit / '.env').write_text('NEVER EXPOSE')
        (self.toolkit / 'output').mkdir()
        (self.toolkit / 'output' / 'customer.md').write_text('PRIVATE')
        self.config = server.Config(root=root / 'private', toolkit=self.toolkit,
            image='registry.example/nova@sha256:' + 'b' * 64, version='a' * 40,
            source_url='https://github.com/noahgsolomon/ldraw-nova/tree/' + 'a' * 40,
            token='a-secret-' * 8)
        self.runner = FakeRunner()
        self.engine = server.Engine(self.config, self.runner)
        self.id = self.engine.create({'files': [{'path': 'generate.py', 'text': 'print(1)'}]})['id']

    def tearDown(self):
        self.temp.cleanup()

    def call(self, **kwargs):
        return self.engine.call(self.id, {'idempotencyKey': 'one', **kwargs})

    def test_full_generator_persists_files_and_images(self):
        result = self.call(action='run', kind='python', path='generate.py', args=[], imagePaths=['preview.png'])
        self.assertIn('Exit code: 0', result['text'])
        self.assertEqual(base64.b64decode(result['images'][0].split(',')[1]), PNG)
        self.assertEqual(self.call(idempotencyKey='read', action='read', path='model.mpd')['text'], '0 Test\n0 STEP\n')

    def test_same_request_runs_once_across_service_reconstruction(self):
        body = dict(idempotencyKey='same', action='run', kind='cli', args=['doctor'])
        first = self.engine.call(self.id, body)
        rebuilt = server.Engine(self.config, self.runner)
        self.assertEqual(rebuilt.call(self.id, body), first)
        self.assertEqual(self.runner.count, 1)

    def test_same_key_different_body_refused(self):
        self.call(action='read', path='generate.py')
        with self.assertRaisesRegex(server.Refused, 'different request'):
            self.call(action='run', kind='cli', args=['doctor'])
        self.assertEqual(self.runner.count, 0)

    def test_failed_request_is_cached(self):
        for _ in range(2):
            with self.assertRaisesRegex(server.Refused, 'missing'):
                self.call(action='read', path='missing')
        meta = json.loads((self.engine.workspaces / self.id / 'meta.json').read_text())
        self.assertEqual(meta['calls'], 1)

    def test_pending_call_never_reexecutes(self):
        body = dict(idempotencyKey='one', action='run', kind='cli', args=['doctor'])
        record = self.engine.workspaces / self.id / 'calls' / 'one.json'
        server.atomic_json(record, {'hash': hashlib.sha256(server.canonical(body)).hexdigest(), 'state': 'started'})
        with self.assertRaisesRegex(server.Refused, 'interrupted'):
            self.engine.call(self.id, body)
        self.assertEqual(self.runner.count, 0)

    def test_concurrent_retries_serialize(self):
        self.runner.gate = threading.Event()
        body = dict(idempotencyKey='one', action='run', kind='cli', args=['doctor'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.engine.call, self.id, body)
            self.assertTrue(self.runner.entered.wait(2))
            second = pool.submit(self.engine.call, self.id, body)
            self.runner.gate.set()
            self.assertEqual(first.result(), second.result())
        self.assertEqual(self.runner.count, 1)

    def test_source_instructions_and_directory_listing(self):
        self.assertEqual(self.call(action='read', source='toolkit', path='instructions.md')['text'], 'Use full toolkit')
        self.assertEqual(self.call(idempotencyKey='list', action='list', source='toolkit', path='docs')['text'], 'guide.md')
        listed = self.call(idempotencyKey='root', action='list', source='toolkit')['text']
        self.assertNotIn('.env', listed)
        self.assertNotIn('output', listed)

    def test_toolkit_private_output_and_writes_refused(self):
        for body in [dict(action='read', source='toolkit', path='output/customer.md'),
                     dict(action='write', source='toolkit', path='instructions.md', text='hijack')]:
            with self.subTest(body=body), self.assertRaises(server.Refused):
                self.call(**body)

    def test_workspace_and_toolkit_symlinks_refused(self):
        with self.engine.locked(self.id) as (directory, meta):
            workspace = directory / meta['generation']
            (workspace / 'escape').symlink_to('/etc/passwd')
        (self.toolkit / 'docs' / 'escape').symlink_to('/etc/passwd')
        for source, path in [('workspace', 'escape'), ('toolkit', 'docs/escape')]:
            with self.subTest(source=source), self.assertRaises(server.Refused):
                self.call(idempotencyKey=source, action='read', source=source, path=path)

    def test_no_arbitrary_executable_or_python_flag(self):
        for body in [dict(action='run', kind='shell', args=['sh']),
                     dict(action='run', kind='python', path='-c', args=['anything']),
                     dict(action='run', kind='python', path='/etc/passwd'),
                     dict(action='run', kind='cli', args=['x\x00y'])]:
            with self.subTest(body=body), self.assertRaises(server.Refused):
                self.call(**body)
        self.assertEqual(self.runner.count, 0)

    def test_relative_paths_only(self):
        for value in ['/etc/passwd', '../secret', 'a/../b', 'a//b', 'a\\b', '.env', 'a/.git/config', 'a\x00b', '', 'a/']:
            with self.subTest(value=value), self.assertRaises(server.Refused):
                self.call(action='write', path=value, text='bad')

    def test_workspace_call_quota_includes_reads(self):
        limited = server.Engine(dataclasses.replace(self.config, max_calls=1), self.runner)
        limited.call(self.id, dict(idempotencyKey='1', action='read', path='generate.py'))
        with self.assertRaisesRegex(server.Refused, 'call limit'):
            limited.call(self.id, dict(idempotencyKey='2', action='read', path='generate.py'))

    def test_wrong_engine_image_refuses_old_workspace(self):
        changed = server.Engine(dataclasses.replace(self.config, image='nova@sha256:' + 'c' * 64), self.runner)
        with self.assertRaisesRegex(server.Refused, 'different engine'):
            changed.call(self.id, dict(idempotencyKey='1', action='read', path='generate.py'))

    def test_write_quota_and_oversized_images_refused(self):
        with mock.patch.object(server, 'MAX_WORKSPACE', 16):
            with self.assertRaisesRegex(server.Refused, 'quota'):
                self.call(action='write', path='large.txt', text='x' * 32)
        with self.assertRaisesRegex(server.Refused, 'at most four'):
            self.call(action='list', imagePaths=['x.png'] * 5)

    def test_delete_removes_cache_and_files(self):
        self.engine.delete(self.id)
        self.assertFalse((self.engine.workspaces / self.id).exists())
        with self.assertRaises(server.Refused):
            self.call(action='list')

    def test_expired_workspaces_reclaimed(self):
        with self.engine.locked(self.id) as (directory, meta):
            meta['expiresAt'] = 0
            server.atomic_json(directory / 'meta.json', meta)
        with self.assertRaisesRegex(server.Refused, 'expired'):
            self.call(action='list')
        self.engine.create({})
        self.assertFalse((self.engine.workspaces / self.id).exists())

    def test_invalid_config_rejected(self):
        for update in [dict(image='nova:latest'), dict(version='main'), dict(source_url='https://user:secret@github.com/x/y'),
                       dict(token='short'), dict(root=self.toolkit / 'private'), dict(timeout=9999)]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                dataclasses.replace(self.config, **update).validate()


    def test_delete_prior_release_and_expired_workspaces(self):
        changed = server.Engine(dataclasses.replace(self.config, image='sha256:' + 'c' * 64), self.runner)
        changed.delete(self.id)
        self.assertFalse((self.engine.workspaces / self.id).exists())

    def test_local_immutable_image_id_accepted(self):
        dataclasses.replace(self.config, image='sha256:' + 'b' * 64).validate()

    def test_cancelled_write_does_not_mutate(self):
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaisesRegex(server.Refused, 'cancelled'):
            self.engine.call(self.id, dict(idempotencyKey='cancelled', action='write', path='new.txt', text='no'), cancelled)
        self.assertNotIn('new.txt', self.call(action='list')['text'])

    def test_toolkit_dot_root_does_not_disclose_output(self):
        result = self.call(action='list', source='toolkit', path='.')
        self.assertNotIn('output', result['text'])

    def test_large_model_text_can_be_exported(self):
        model = '0 model line\n' * 10000
        self.call(action='write', path='large.mpd', text=model)
        self.assertEqual(self.call(idempotencyKey='readlarge', action='read', path='large.mpd')['text'], model)


class ArchiveTest(unittest.TestCase):
    def test_valid_archive_ignores_ownership_and_executable_modes(self):
        with tempfile.TemporaryDirectory() as root:
            server.import_archive(io.BytesIO(archive([('./nested/model.mpd', b'0 model', tarfile.REGTYPE)])), Path(root))
            self.assertEqual((Path(root) / 'nested' / 'model.mpd').read_text(), '0 model')

    def test_all_link_and_special_file_types_refused(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root, self.assertRaises(server.Refused):
                server.import_archive(io.BytesIO(archive([('bad', b'', kind)])), Path(root))

    def test_traversal_absolute_hidden_and_duplicate_entries_refused(self):
        entries = [[(name, b'x', tarfile.REGTYPE)] for name in ['../escape', '/escape', 'a/../../escape', '.env', 'a\\b']]
        entries += [[('same', b'x', tarfile.REGTYPE), ('same', b'y', tarfile.REGTYPE)]]
        for entry in entries:
            with self.subTest(entry=entry), tempfile.TemporaryDirectory() as root, self.assertRaises(server.Refused):
                server.import_archive(io.BytesIO(archive(entry)), Path(root))

    def test_archive_budget_checked_before_file_materialization(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(server, 'MAX_FILE', 2):
            with self.assertRaisesRegex(server.Refused, 'size limit'):
                server.import_archive(io.BytesIO(archive([('big', b'123', tarfile.REGTYPE)])), Path(root))
            self.assertFalse((Path(root) / 'big').exists())


class DockerTest(EngineTest):
    def test_docker_flags_environment_and_cleanup(self):
        runner = server.DockerRunner(self.config)
        calls = []
        def fake_process(args, **kwargs):
            calls.append((args, kwargs))
            if args[:1] == ['exec'] and '--workdir' in args:
                return 0, b'success'
            if args[:1] == ['cp'] and args[-1] == '-':
                return 0, archive([('result.mpd', b'0 model', tarfile.REGTYPE)])
            return 0, b''
        with mock.patch.object(runner, 'process', side_effect=fake_process):
            with self.engine.locked(self.id) as (directory, meta):
                code, text, stage = runner.run(directory / meta['generation'], dict(kind='cli', args=['doctor']), threading.Event())
        command = calls[0][0]
        self.assertEqual(code, 0)
        for required in ['--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                         '--pids-limit=128', '--memory=1g', '--memory-swap=1g', '--user', '65532:65532', '--pull=never']:
            self.assertIn(required, command)
        self.assertNotIn('--mount', command)
        self.assertNotIn('--volume', command)
        self.assertIn('/job:rw,nosuid,nodev,noexec,size=64m,mode=1777', command)
        self.assertEqual(set(runner.environment), {'PATH', 'HOME', 'LANG'})
        self.assertNotIn(self.config.token, str(command))
        self.assertEqual(calls[-1][0][:2], ['rm', '--force'])
        self.assertEqual((stage / 'result.mpd').read_text(), '0 model')

    def test_cleanup_on_execution_error(self):
        runner = server.DockerRunner(self.config)
        calls = []
        def failure(args, **kwargs):
            calls.append(args)
            if args[:1] == ['exec'] and '--workdir' in args:
                raise server.Refused('timeout', 504)
            return 0, b''
        with mock.patch.object(runner, 'process', side_effect=failure), self.engine.locked(self.id) as (directory, meta):
            with self.assertRaisesRegex(server.Refused, 'timeout'):
                runner.run(directory / meta['generation'], dict(kind='cli', args=['doctor']), threading.Event())
        self.assertEqual(calls[-1][:2], ['rm', '--force'])
        self.assertFalse(runner.active)

    def test_real_process_output_bound_and_timeout_cleanup(self):
        runner = server.DockerRunner(self.config)
        import sys
        with mock.patch.object(runner, 'command', return_value=[sys.executable, '-c', 'print("x" * 10000)']):
            with self.assertRaisesRegex(server.Refused, 'output exceeded'):
                runner.process([], limit=100)
        with mock.patch.object(runner, 'command', return_value=[sys.executable, '-c', 'import time; time.sleep(10)']):
            with self.assertRaisesRegex(server.Refused, 'timed out'):
                runner.process([], timeout=0.1)

    def test_preflight_refuses_mismatched_public_source(self):
        runner = server.DockerRunner(self.config)
        with mock.patch.object(runner, 'checked', return_value=b'[{"Config":{"Labels":{}}}]'):
            with self.assertRaisesRegex(ValueError, 'labels'):
                runner.preflight()


    def test_failed_container_cleanup_keeps_capacity_and_recovers(self):
        runner = server.DockerRunner(self.config)
        name = 'nova-job-' + 'c' * 32
        runner.slots.acquire()
        runner.active.add(name)
        with mock.patch.object(runner, 'process', return_value=(1, b'Docker unavailable')):
            self.assertFalse(runner.remove(name))
        self.assertFalse(runner.healthy())
        self.assertIn(name, runner.active)
        with mock.patch.object(runner, 'process', return_value=(0, b'')):
            runner.reap_orphans()
        self.assertTrue(runner.healthy())
        self.assertFalse(runner.active)

    def test_pause_precedes_archive_export(self):
        runner = server.DockerRunner(self.config)
        calls = []
        def fake(args, **kwargs):
            calls.append(args)
            if args[:1] == ['cp'] and args[-1] == '-':
                return 0, archive([('result.mpd', b'0 model', tarfile.REGTYPE)])
            return 0, b''
        with mock.patch.object(runner, 'process', side_effect=fake), self.engine.locked(self.id) as (directory, meta):
            runner.run(directory / meta['generation'], dict(kind='cli', args=['doctor']), threading.Event())
        pause = next(i for i, args in enumerate(calls) if args[0] == 'pause')
        export = next(i for i, args in enumerate(calls) if args[0] == 'cp' and args[-1] == '-')
        self.assertLess(pause, export)


class HttpTest(EngineTest):
    def setUp(self):
        super().setUp()
        self.http = server.Server(('127.0.0.1', 0), self.engine)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, method, path, body=None, token=None):
        client = http.client.HTTPConnection(*self.http.server_address, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        client.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = client.getresponse()
        status, data = response.status, json.loads(response.read())
        client.close()
        return status, data

    def test_health_and_source_offer_are_public_but_workspaces_private(self):
        for endpoint in ['/health', '/source']:
            status, body = self.request('GET', endpoint)
            self.assertEqual(status, 200)
            self.assertEqual(body['sourceUrl'], self.config.source_url)
            self.assertEqual(body['engineVersion'], self.config.version)
        status, _ = self.request('POST', '/v1/workspaces', {'files': []})
        self.assertEqual(status, 401)
        status, _ = self.request('POST', '/v1/workspaces', {'files': []}, token='wrong')
        self.assertEqual(status, 401)

    def test_authenticated_readiness_checks_credentials_without_execution(self):
        self.assertEqual(self.request('GET', '/v1/ready')[0], 401)
        self.assertEqual(self.request('GET', '/v1/ready', token='wrong')[0], 401)
        status, body = self.request('GET', '/v1/ready', token=self.config.token)
        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        self.assertEqual(body['engineVersion'], self.config.version)
        self.assertEqual(self.runner.count, 0)
        with mock.patch.object(self.runner, 'healthy', return_value=False, create=True):
            self.assertEqual(self.request('GET', '/v1/ready', token=self.config.token)[0], 503)

    def test_complete_authenticated_http_flow(self):
        status, body = self.request('POST', '/v1/workspaces', {'files': []}, self.config.token)
        self.assertEqual(status, 201)
        id = body['id']
        status, result = self.request('POST', f'/v1/workspaces/{id}/calls',
            {'idempotencyKey': 'write', 'action': 'write', 'path': 'a.txt', 'text': 'hello'}, self.config.token)
        self.assertEqual(status, 200)
        status, result = self.request('POST', f'/v1/workspaces/{id}/calls',
            {'idempotencyKey': 'read', 'action': 'read', 'path': 'a.txt'}, self.config.token)
        self.assertEqual(result, {'text': 'hello'})
        status, _ = self.request('DELETE', f'/v1/workspaces/{id}', token=self.config.token)
        self.assertEqual(status, 200)


if __name__ == '__main__':
    unittest.main()
