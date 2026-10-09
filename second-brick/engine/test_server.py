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
                       dict(token='short'), dict(root=self.toolkit / 'private'), dict(timeout=9999),
                       dict(create_timeout=0), dict(create_timeout=121), dict(create_timeout=31, timeout=30)]:
            with self.subTest(update=update), self.assertRaises(ValueError):
                dataclasses.replace(self.config, **update).validate()

    def test_creation_timeout_defaults_and_supported_bounds(self):
        self.assertEqual(self.config.create_timeout, 30)
        for create_timeout in (1, 90, 120):
            with self.subTest(create_timeout=create_timeout):
                dataclasses.replace(self.config, create_timeout=create_timeout).validate()


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
    def test_input_archive_normalizes_ownership_modes_and_preserves_empty_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'empty').mkdir()
            (root / 'nested').mkdir()
            (root / 'nested' / 'generate.py').write_text('print(1)')
            os.chmod(root / 'nested' / 'generate.py', 0o777)
            with server.workspace_archive(root) as source, tarfile.open(fileobj=source) as packed:
                members = {item.name: item for item in packed}
                self.assertEqual(set(members), {'.', 'empty', 'nested', 'nested/generate.py'})
                for member in members.values():
                    self.assertEqual((member.uid, member.gid), (65532, 65532))
                    self.assertEqual(member.mode, 0o700 if member.isdir() else 0o600)
                    self.assertEqual((member.uname, member.gname), ('', ''))
                self.assertEqual(packed.extractfile(members['nested/generate.py']).read(), b'print(1)')

    def test_input_archive_refuses_links_special_files_and_oversized_files(self):
        for kind in ('symlink', 'hardlink', 'fifo', 'oversized'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'source').write_bytes(b'123')
                if kind == 'symlink':
                    (root / 'unsafe').symlink_to(root / 'source')
                elif kind == 'hardlink':
                    os.link(root / 'source', root / 'unsafe')
                elif kind == 'fifo':
                    os.mkfifo(root / 'unsafe')
                with mock.patch.object(server, 'MAX_FILE', 2 if kind == 'oversized' else server.MAX_FILE):
                    with self.assertRaises(server.Refused), server.workspace_archive(root):
                        self.fail('Unsafe input must never reach Docker')

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
            if args[:2] == ['volume', 'create'] or args[0] == 'run':
                name = ('nova-job-' + args[-1].removeprefix('nova-work-')) if args[0] == 'volume' else args[args.index('--name') + 1]
                journal = json.loads((runner.reservation_dir / (name + '.json')).read_text())
                self.assertEqual(journal['volume' if args[0] == 'volume' else 'container'], 'pending')
            if args[:1] == ['exec'] and '--workdir' in args:
                return 0, b'success'
            if args[:1] == ['cp'] and args[-1] == '-':
                return 0, archive([('result.mpd', b'0 model', tarfile.REGTYPE)])
            return 0, b''
        with mock.patch.object(runner, 'process', side_effect=fake_process):
            with self.engine.locked(self.id) as (directory, meta):
                code, text, stage = runner.run(directory / meta['generation'], dict(kind='cli', args=['doctor']), threading.Event())
        command, create_options = next((args, options) for args, options in calls if args[0] == 'run')
        volume_command = calls[0][0]
        volume = volume_command[-1]
        self.assertEqual(code, 0)
        self.assertEqual(create_options['timeout'], 30)
        self.assertEqual(calls[0][1]['timeout'], 10)
        self.assertEqual(volume_command[:4], ['volume', 'create', '--driver', 'local'])
        self.assertIn('org.secondbrick.instance=' + runner.instance, volume_command)
        self.assertIn('org.secondbrick.engine=isolated-workspace', volume_command)
        for option in ('type=tmpfs', 'device=tmpfs', 'o=size=64m,nosuid,nodev,noexec,mode=0700,uid=65532,gid=65532'):
            self.assertIn(option, volume_command)
        for required in ['--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
                         '--pids-limit=128', '--memory=1g', '--memory-swap=1g', '--user', '65532:65532', '--pull=never']:
            self.assertIn(required, command)
        self.assertNotIn('--volume', command)
        self.assertEqual(command[command.index('--mount') + 1], f'type=volume,source={volume},target=/job,volume-nocopy')
        self.assertNotIn('type=bind', str(command))
        self.assertIn('/opt/nova/.cache:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=65532,gid=65532', command)
        self.assertIn(f'fsize={128 * server.MiB}:{128 * server.MiB}', command)
        self.assertEqual(set(runner.environment), {'PATH', 'HOME', 'LANG'})
        self.assertNotIn(self.config.token, str(command))
        self.assertEqual(calls[-2][0][:2], ['rm', '--force'])
        self.assertEqual(calls[-1][0], ['volume', 'rm', volume])
        incoming = next(i for i, (args, _) in enumerate(calls) if args[:3] == ['cp', '--archive', '-'])
        self.assertIn('stdin', calls[incoming][1])
        initialize = next(i for i, (args, _) in enumerate(calls) if args[0] == 'exec' and 'copytree' in args[-1])
        author = next(i for i, (args, _) in enumerate(calls) if args[0] == 'exec' and '--workdir' in args)
        self.assertLess(incoming, initialize)
        self.assertLess(initialize, author)
        self.assertFalse(any('0:0' in args for args, _ in calls))
        self.assertNotIn('/bin/chmod', str(calls))
        self.assertFalse(runner.volumes)
        self.assertEqual(list(runner.reservation_dir.glob('*.json')), [])
        self.assertEqual((stage / 'result.mpd').read_text(), '0 model')

    def test_configured_creation_timeout_keeps_total_deadline_and_cancellation(self):
        runner = server.DockerRunner(dataclasses.replace(self.config, create_timeout=90))
        cancelled = threading.Event()
        calls = []
        def fake_process(args, **kwargs):
            calls.append((args, kwargs))
            if args[:2] in (['volume', 'create'], ['volume', 'rm']):
                return 0, b''
            if args[0] == 'run':
                self.assertEqual(kwargs['timeout'], 90)
                self.assertNotIn('cancelled', kwargs)
                # The total run timer can expire during creation. Its existing
                # cancellation event must still prevent subsequent author code.
                cancelled.set()
                return 0, b''
            if kwargs.get('cancelled') is not None and kwargs['cancelled'].is_set():
                raise server.Refused('Engine command cancelled or timed out', 504)
            self.assertEqual(args[:2], ['rm', '--force'])
            return 0, b''
        with mock.patch.object(server.threading, 'Timer') as timer, \
                mock.patch.object(runner, 'process', side_effect=fake_process):
            with self.assertRaisesRegex(server.Refused, 'timed out'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), cancelled)
        timer.assert_called_once_with(120, cancelled.set)
        timer.return_value.start.assert_called_once()
        timer.return_value.cancel.assert_called_once()
        self.assertFalse(any(args[0] == 'exec' for args, _ in calls))
        self.assertEqual(calls[-2][0][:2], ['rm', '--force'])
        self.assertEqual(calls[-1][0][:2], ['volume', 'rm'])
        self.assertFalse(runner.active)

    def test_shutdown_budget_follows_creation_timeout(self):
        for create_timeout, budget in ((30, 65), (90, 125), (120, 155)):
            with self.subTest(create_timeout=create_timeout):
                runner = server.DockerRunner(dataclasses.replace(self.config, create_timeout=create_timeout))
                cancelled = threading.Event()
                runner.running['pending-creation'] = cancelled
                # Advancing the clock proves the deadline without sleeping for
                # up to 155 seconds or falsely claiming that creation drained.
                with mock.patch.object(server.time, 'monotonic', side_effect=(100, 100, 100 + budget)), \
                        mock.patch.object(runner.drained, 'wait') as wait, \
                        mock.patch.object(runner, 'process') as process:
                    self.assertFalse(runner.close())
                wait.assert_called_once_with(budget)
                process.assert_not_called()
                self.assertTrue(cancelled.is_set())

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
        self.assertEqual(calls[-2][:2], ['rm', '--force'])
        self.assertEqual(calls[-1][:2], ['volume', 'rm'])
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

    def test_process_reads_regular_file_input_without_a_writer_thread(self):
        runner = server.DockerRunner(self.config)
        import sys
        with tempfile.TemporaryFile() as source:
            source.write(b'bounded input\x00')
            source.seek(0)
            with mock.patch.object(runner, 'command', return_value=[sys.executable, '-c',
                    'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())']):
                self.assertEqual(runner.process([], stdin=source), (0, b'bounded input\x00'))
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        with os.fdopen(read_fd, 'rb') as pipe, mock.patch.object(server.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(server.Refused, 'ordinary file'):
                runner.process([], stdin=pipe)
            spawn.assert_not_called()

    def test_preflight_refuses_mismatched_public_source(self):
        runner = server.DockerRunner(self.config)
        with mock.patch.object(runner, 'checked', return_value=b'[{"Config":{"Labels":{}}}]'):
            with self.assertRaisesRegex(ValueError, 'labels'):
                runner.preflight()

    def test_preflight_reclaims_labeled_containers_before_workspace_volumes(self):
        runner = server.DockerRunner(self.config)
        name, volume = 'nova-job-' + 'c' * 32, 'nova-work-' + 'c' * 32
        calls = []
        def fake(args, **kwargs):
            calls.append(args)
            if args[:2] == ['image', 'inspect']:
                return json.dumps([{'Config': {'Labels': {
                    'org.opencontainers.image.revision': self.config.version,
                    'org.opencontainers.image.source': self.config.source_url.rsplit('/', 2)[0],
                }}}]).encode()
            if args[0] == 'ps':
                return (name + '\nunrelated-container\n').encode()
            if args[:2] == ['volume', 'ls']:
                self.assertIn('label=org.secondbrick.instance=' + runner.instance, args)
                self.assertIn('label=org.secondbrick.engine=isolated-workspace', args)
                return (volume + '\nunrelated-volume\n').encode()
            return b''
        with mock.patch.object(runner, 'checked', side_effect=fake):
            runner.preflight()
        removals = [args for args in calls if args[0] == 'rm' or args[:2] == ['volume', 'rm']]
        self.assertEqual(removals, [['rm', '--force', name], ['volume', 'rm', volume]])

    def test_restart_preserves_pending_creation_despite_empty_docker_scans(self):
        for pending in ('volume', 'container'):
            with self.subTest(pending=pending):
                config = dataclasses.replace(self.config, root=self.config.root / pending, parallel=1)
                config.root.mkdir()
                original = server.DockerRunner(config)
                name = 'nova-job-' + 'c' * 32
                original.record_resource(name, 'volume', 'pending' if pending == 'volume' else 'confirmed')
                if pending == 'container':
                    original.record_resource(name, 'container', 'pending')
                # A replacement knows only durable records. Docker's label scan
                # is empty because the original create RPC is still in flight.
                replacement = server.DockerRunner(config)
                calls = []
                def missing(args, **kwargs):
                    calls.append(args)
                    if args[:2] == ['image', 'inspect']:
                        return 0, json.dumps([{'Config': {'Labels': {
                            'org.opencontainers.image.revision': config.version,
                            'org.opencontainers.image.source': config.source_url.rsplit('/', 2)[0],
                        }}}]).encode()
                    if args[0] == 'rm':
                        return 1, b'No such container'
                    if args[:2] == ['volume', 'rm']:
                        return 1, b'no such volume'
                    return 0, b''
                with mock.patch.object(replacement, 'process', side_effect=missing):
                    replacement.preflight()
                    self.assertFalse(replacement.close(timeout=0.02))
                self.assertEqual(replacement.active, {name})
                self.assertFalse(replacement.healthy())
                self.assertFalse(replacement.slots.acquire(blocking=False))
                self.assertTrue((replacement.reservation_dir / (name + '.json')).exists())
                if pending == 'volume':
                    self.assertFalse(any(args[0] == 'rm' for args in calls), 'No container creation was attempted.')
                else:
                    self.assertFalse(any(args[:2] == ['volume', 'rm'] for args in calls), 'A late container may still mount the volume.')
                # Once the late resource actually appears, successful removal
                # clears the marker; then a new service can safely accept work.
                with mock.patch.object(replacement, 'process', return_value=(0, b'')):
                    replacement.reap_orphans()
                self.assertFalse(replacement.active)
                self.assertEqual(list(replacement.reservation_dir.glob('*.json')), [])

    def test_restart_reclaims_confirmed_resources_that_are_already_absent(self):
        original = server.DockerRunner(self.config)
        name = 'nova-job-' + 'd' * 32
        original.record_resource(name, 'volume', 'confirmed')
        original.record_resource(name, 'container', 'confirmed')
        replacement = server.DockerRunner(self.config)
        def missing(args, **kwargs):
            return (1, b'No such container') if args[0] == 'rm' else (1, b'no such volume')
        with mock.patch.object(replacement, 'process', side_effect=missing):
            replacement.recover_reservations()
        self.assertTrue(replacement.healthy())
        self.assertFalse(replacement.active)
        self.assertFalse(replacement.reservations)
        self.assertEqual(list(replacement.reservation_dir.glob('*.json')), [])

    def test_journal_write_failure_prevents_creation(self):
        runner = server.DockerRunner(self.config)
        with mock.patch.object(server, 'atomic_json', side_effect=OSError('journal unavailable')), \
                mock.patch.object(runner, 'process', return_value=(1, b'No such container')) as process:
            with self.assertRaisesRegex(OSError, 'journal unavailable'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), threading.Event())
        self.assertFalse(any(call.args[0][0] in ('run', 'volume') for call in process.call_args_list))
        self.assertFalse(runner.active)

    def test_removal_journal_failure_retains_reservation_and_capacity(self):
        runner = server.DockerRunner(dataclasses.replace(self.config, parallel=1))
        name = 'nova-job-' + 'd' * 32
        runner.record_resource(name, 'volume', 'confirmed')
        runner.record_resource(name, 'container', 'confirmed')
        runner.active.add(name)
        runner.slots.acquire()
        with mock.patch.object(server, 'atomic_json', side_effect=OSError('journal unavailable')), \
                mock.patch.object(runner, 'process', return_value=(0, b'')):
            self.assertFalse(runner.remove(name))
        self.assertFalse(runner.healthy())
        self.assertFalse(runner.slots.acquire(blocking=False))
        self.assertTrue((runner.reservation_dir / (name + '.json')).exists())

    def test_invalid_reservation_fails_startup_closed(self):
        runner = server.DockerRunner(self.config)
        server.atomic_json(runner.reservation_dir / ('nova-job-' + 'd' * 32 + '.json'), {'container': 'confirmed'})
        with mock.patch.object(runner, 'process') as process:
            with self.assertRaisesRegex(ValueError, 'Invalid Docker reservation'):
                runner.recover_reservations()
            process.assert_not_called()


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

    def test_failed_volume_cleanup_keeps_capacity_after_container_removal(self):
        runner = server.DockerRunner(dataclasses.replace(self.config, parallel=1))
        name, volume = 'nova-job-' + 'c' * 32, 'nova-work-' + 'c' * 32
        runner.slots.acquire()
        runner.active.add(name)
        runner.volumes[name] = volume
        def failed_volume(args, **kwargs):
            return (1, b'volume is in use') if args[0] == 'volume' else (0, b'')
        with mock.patch.object(runner, 'process', side_effect=failed_volume):
            self.assertFalse(runner.remove(name))
        self.assertFalse(runner.healthy())
        self.assertEqual(runner.volumes[name], volume)
        self.assertFalse(runner.slots.acquire(blocking=False))
        def recovered(args, **kwargs):
            return (1, b'No such container') if args[0] == 'rm' else (0, b'')
        with mock.patch.object(runner, 'process', side_effect=recovered):
            runner.reap_orphans()
        self.assertTrue(runner.healthy())
        self.assertFalse(runner.active)
        self.assertFalse(runner.volumes)
        self.assertTrue(runner.slots.acquire(blocking=False))
        runner.slots.release()

    def test_uncertain_volume_creation_waits_for_actual_late_volume_removal(self):
        runner = server.DockerRunner(dataclasses.replace(self.config, parallel=1))
        calls = []
        def interrupted_create(args, **kwargs):
            calls.append(args)
            if args[:2] == ['volume', 'create']:
                self.assertNotIn('cancelled', kwargs)
                raise server.Refused('Volume creation timed out', 504)
            if args[0] == 'rm':
                return 1, b'No such container'
            self.assertEqual(args[:2], ['volume', 'rm'])
            self.assertNotIn('--force', args, 'Force can hide a missing volume and falsely confirm late-create cleanup.')
            return 1, b'Error response from daemon: no such volume'
        with mock.patch.object(runner, 'process', side_effect=interrupted_create):
            with self.assertRaisesRegex(server.Refused, 'Volume creation timed out'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), threading.Event())
            runner.reap_orphans()
        self.assertFalse(any(args[0] in ('run', 'exec') for args in calls))
        self.assertFalse(runner.healthy())
        self.assertEqual(runner.active, runner.uncertain_volumes)
        self.assertFalse(runner.slots.acquire(blocking=False))
        def late_volume_removed(args, **kwargs):
            return (1, b'No such container') if args[0] == 'rm' else (0, b'')
        with mock.patch.object(runner, 'process', side_effect=late_volume_removed):
            runner.reap_orphans()
        self.assertTrue(runner.healthy())
        self.assertFalse(runner.active)
        self.assertFalse(runner.volumes)
        self.assertFalse(runner.uncertain_volumes)

    def test_shutdown_waits_for_pending_volume_without_starting_container(self):
        runner = server.DockerRunner(self.config)
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        events = []
        def fake(args, **kwargs):
            if args[:2] == ['volume', 'create']:
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('Test volume creation gate timed out')
                events.append('volume created')
                return 0, b''
            if args[0] == 'rm':
                return 1, b'No such container'
            self.assertEqual(args[:2], ['volume', 'rm'])
            events.append('volume removed')
            return 0, b''
        def command():
            with self.assertRaisesRegex(server.Refused, 'cancelled'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), cancelled)
        with mock.patch.object(runner, 'process', side_effect=fake), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(command)
            try:
                self.assertTrue(entered.wait(1))
                closing = pool.submit(runner.close)
                self.assertTrue(cancelled.wait(1))
                self.assertFalse(closing.done())
                self.assertEqual(events, [])
            finally:
                release.set()
            running.result(timeout=2)
            self.assertTrue(closing.result(timeout=2))
        self.assertEqual(events, ['volume created', 'volume removed'])
        self.assertFalse(runner.active)
        self.assertFalse(runner.volumes)

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

    def test_closed_runner_refuses_new_commands_without_touching_docker(self):
        runner = server.DockerRunner(self.config)
        self.assertTrue(runner.close())
        self.assertFalse(runner.healthy())
        with mock.patch.object(runner, 'process') as process:
            with self.assertRaisesRegex(server.Refused, 'shutting down'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), threading.Event())
            process.assert_not_called()

    def test_queued_request_cannot_start_after_shutdown_releases_capacity(self):
        runner = server.DockerRunner(dataclasses.replace(self.config, parallel=1))
        runner.slots.acquire()  # Capacity is occupied before the request arrives.
        waiting = threading.Event()
        acquire = runner.slots.acquire
        def wait_for_capacity(*args, **kwargs):
            waiting.set()
            return acquire(*args, **kwargs)
        def queued():
            with self.assertRaisesRegex(server.Refused, 'shutting down'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), threading.Event())
        with mock.patch.object(runner.slots, 'acquire', side_effect=wait_for_capacity), \
                mock.patch.object(runner, 'process') as process, ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(queued)
            try:
                self.assertTrue(waiting.wait(1))
                self.assertTrue(runner.close())
            finally:
                runner.slots.release()
            request.result(timeout=2)
            process.assert_not_called()
        self.assertFalse(runner.active)
        self.assertFalse(runner.running)

    def test_shutdown_waits_for_creation_then_removes_before_returning(self):
        runner = server.DockerRunner(self.config)
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        events = []
        def fake(args, **kwargs):
            if args[:2] == ['volume', 'create']:
                return 0, b''
            if args[:2] == ['volume', 'rm']:
                events.append('volume removed')
                return 0, b''
            if args[0] == 'run':
                self.assertNotIn('cancelled', kwargs, 'Do not abandon daemon-side creation on client cancellation.')
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('Test creation gate timed out')
                events.append('created')
                return 0, b''
            if args[0] == 'rm':
                events.append('removed')
                return 0, b''
            if kwargs.get('cancelled') is not None and kwargs['cancelled'].is_set():
                raise server.Refused('Engine command cancelled or timed out', 504)
            self.fail('Author execution must not start after shutdown')
        def command():
            with self.assertRaisesRegex(server.Refused, 'cancelled'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), cancelled)
        with mock.patch.object(runner, 'process', side_effect=fake), ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(command)
            try:
                self.assertTrue(entered.wait(1))
                closing = pool.submit(runner.close)
                self.assertTrue(cancelled.wait(1))
                self.assertFalse(closing.done())
                self.assertEqual(events, [], 'No early rm while Docker has not confirmed creation.')
            finally:
                release.set()
            running.result(timeout=2)
            self.assertTrue(closing.result(timeout=2))
        self.assertEqual(events, ['created', 'removed', 'volume removed'])
        self.assertFalse(runner.active)
        self.assertFalse(runner.running)

    def test_shutdown_timeout_is_bounded_and_does_not_claim_cleanup(self):
        runner = server.DockerRunner(self.config)
        cancelled = threading.Event()
        name = 'nova-job-' + 'c' * 32
        runner.slots.acquire()
        runner.active.add(name)
        runner.running[name] = cancelled
        with mock.patch.object(runner, 'process') as process:
            started = time.monotonic()
            self.assertFalse(runner.close(timeout=0.02))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(cancelled.is_set())
            self.assertFalse(runner.healthy())
            process.assert_not_called()  # An unresolved creator owns its cleanup.
        with runner.drained:
            runner.running.clear()
            runner.drained.notify_all()
        with mock.patch.object(runner, 'process', return_value=(0, b'')):
            self.assertTrue(runner.close())

    def test_uncertain_creation_keeps_capacity_until_late_container_is_removed(self):
        runner = server.DockerRunner(dataclasses.replace(self.config, parallel=1))
        def interrupted_create(args, **kwargs):
            if args[:2] == ['volume', 'create']:
                return 0, b''
            if args[0] == 'run':
                raise server.Refused('Docker creation timed out', 504)
            self.assertEqual(args[:2], ['rm', '--force'])
            return 1, b'No such container'
        with mock.patch.object(runner, 'process', side_effect=interrupted_create):
            with self.assertRaisesRegex(server.Refused, 'creation timed out'):
                runner.run(self.config.root, dict(kind='cli', args=['doctor']), threading.Event())
            runner.reap_orphans()
        self.assertFalse(runner.healthy())
        self.assertEqual(len(runner.active), 1)
        self.assertEqual(runner.active, runner.orphans)
        self.assertEqual(runner.active, runner.uncertain_starts)
        self.assertFalse(runner.slots.acquire(blocking=False))
        self.assertFalse(runner.running)
        # Docker eventually completed the timed-out creation; a real successful
        # removal now proves the named container cannot continue running.
        with mock.patch.object(runner, 'process', return_value=(0, b'')):
            runner.reap_orphans()
        self.assertTrue(runner.healthy())
        self.assertFalse(runner.active)
        self.assertFalse(runner.uncertain_starts)
        self.assertTrue(runner.slots.acquire(blocking=False))
        runner.slots.release()

    def test_cancelled_process_does_not_spawn_a_docker_client(self):
        runner = server.DockerRunner(self.config)
        cancelled = threading.Event()
        cancelled.set()
        with mock.patch.object(server.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(server.Refused, 'cancelled'):
                runner.process(['run', 'unused'], cancelled=cancelled)
            spawn.assert_not_called()


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
