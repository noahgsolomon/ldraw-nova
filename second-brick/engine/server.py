#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded workspace API for the complete Nova toolkit. Python 3.12+, Linux.

The host only stores opaque files and orchestrates Docker. Author code never runs
in this process. Each command gets a fresh, offline, read-only, unprivileged
container. Its writable workspace is a size-limited tmpfs, never a host bind.
"""
from __future__ import annotations

import base64
import contextlib
import dataclasses
import fcntl
import hashlib
import hmac
import http.server
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid

MiB = 1024 * 1024
MAX_FILE = 8 * MiB
MAX_WORKSPACE = 32 * MiB
MAX_FILES = 2048
MAX_REQUEST = 10 * MiB
MAX_TEXT = MiB
MAX_READ_TEXT = 5 * MiB
MAX_OUTPUT = 64 * 1024
MAX_IMAGE = 2 * MiB
MAX_IMAGES = 4
MAX_RESPONSE = 16 * MiB
# JSON's ASCII encoding can use six bytes for one UTF-8 input byte (for
# example, a NUL). Preserve complete admitted models even at that boundary.
MAX_READ_RESPONSE = MAX_READ_TEXT * 6 + MAX_IMAGES * ((MAX_IMAGE + 2) // 3 * 4) + 4096
MAX_CACHE = 64 * MiB
MAX_ARGS = 128
VOLUME_CREATE_TIMEOUT = 10
CLEANUP_TIMEOUT = 20
RUNTIME_FILE_LIMIT = 128 * MiB
PUBLIC_ROOTS = frozenset({
    'ATTRIBUTION.md', 'CC-BY-SA-4.0', 'LICENSE', 'README.md', 'VARIANT_REPORT.md',
    'instructions.md', 'ldraw-agent', 'pyproject.toml', 'uv.lock', 'check-model.sh',
    'prepare-glb.sh', 'setup.sh', 'verify_endplate.py', 'data', 'docs', 'examples',
    'img', 'ldraw_tools', 'prompts', 'second-brick', 'tests',
})
ID = re.compile(r'^[a-f0-9]{32}$')
KEY = re.compile(r'^[A-Za-z0-9_-]{1,96}$')


class Refused(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def path_parts(value, *, root=False):
    if not isinstance(value, str) or len(value) > 512 or '\\' in value or '\x00' in value:
        raise Refused('Invalid relative path')
    if root and value in ('', '.'):
        return ()
    parts = value.split('/')
    if not parts or any(not p or p in ('.', '..') or p.startswith('.') or any(ord(c) < 32 for c in p) for p in parts):
        raise Refused('Paths must be relative and contain no hidden, empty or parent components')
    if PurePosixPath(value).is_absolute():
        raise Refused('Absolute paths are not accepted')
    return tuple(parts)


def secure_fd(root: Path, path: str, *, directory=False):
    """Walk with openat/O_NOFOLLOW; do not resolve and then reopen a path."""
    parts = path_parts(path, root=directory)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for i, component in enumerate(parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if i < len(parts) - 1 or directory:
                flags |= os.O_DIRECTORY
            nxt = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = nxt
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise Refused('Only ordinary files and directories are supported')
        return fd
    except (OSError, Refused):
        os.close(fd)
        raise Refused('Path is missing or is not a safe ordinary file/directory', 404)


def read_bytes(root, path, limit=MAX_FILE):
    fd = secure_fd(root, path)
    with os.fdopen(fd, 'rb') as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise Refused('File exceeds the read limit', 413)
        value = stream.read(limit + 1)
        if len(value) > limit:
            raise Refused('File exceeds the read limit', 413)
        return value


def atomic_json(path: Path, value):
    data = canonical(value)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _inventory(root: Path):
    """Host workspace is never mounted: only this process can alter its files."""
    result, total, entries = [], 0, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            entries += 1
            if entries > MAX_FILES * 2:
                raise Refused('Workspace entry quota exceeded', 413)
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            path_parts(relative)
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_nlink > 1 and stat.S_ISREG(info.st_mode):
                raise Refused('Workspace contains a link or special file')
            if stat.S_ISREG(info.st_mode):
                if info.st_size > MAX_FILE:
                    raise Refused('Workspace file exceeds size limit', 413)
                total += info.st_size
                result.append((relative, info.st_size))
                if len(result) > MAX_FILES or total > MAX_WORKSPACE:
                    raise Refused('Workspace quota exceeded', 413)
    return sorted(result), total, entries


def inventory(root: Path):
    files, total, _ = _inventory(root)
    return files, total


def put_file(root: Path, path, data):
    parts = path_parts(path)
    if len(data) > MAX_FILE:
        raise Refused('File exceeds size limit', 413)
    # No untrusted process ever writes the host store; each caller holds its lock.
    files, size, entries = _inventory(root)
    parent = root
    missing = []
    for part in parts[:-1]:
        parent = parent / part
        if parent.is_symlink() or parent.exists() and not parent.is_dir():
            raise Refused('Parent is not an ordinary directory')
        if not parent.exists():
            missing.append(parent)
    target = parent / parts[-1]
    if target.is_symlink() or target.exists() and not target.is_file():
        raise Refused('Destination is not an ordinary file')
    exists = target.exists()
    previous = target.stat().st_size if exists else 0
    if entries + len(missing) + int(not exists) > MAX_FILES * 2:
        raise Refused('Workspace entry quota exceeded', 413)
    if size - previous + len(data) > MAX_WORKSPACE or len(files) + int(not exists) > MAX_FILES:
        raise Refused('Workspace quota exceeded', 413)
    temp = parent / f'.write-{uuid.uuid4().hex}'
    created = []
    try:
        for directory in missing:
            directory.mkdir(mode=0o700)
            created.append(directory)
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
        # A refused/failed write must not accumulate empty parent directories.
        # On success these directories contain the published file, so rmdir
        # leaves them intact; existing directories never enter this list.
        for directory in reversed(created):
            with contextlib.suppress(OSError):
                directory.rmdir()


def import_archive(stream, destination: Path):
    """Never extractall. Reject links/devices and validate bytes before publishing."""
    seen, total, count = set(), 0, 0
    with tarfile.open(fileobj=stream, mode='r|*') as archive:
        for member in archive:
            name = member.name
            # docker cp emits a conventional ./ prefix and root directory.
            if name in ('.', './') and member.isdir():
                continue
            if name.startswith('./'):
                name = name[2:]
            if member.isdir():
                name = name.rstrip('/')
            parts = path_parts(name)
            if name in seen:
                raise Refused('Archive has duplicate entries')
            seen.add(name)
            count += 1
            if count > MAX_FILES * 2 or not (member.isfile() or member.isdir()):
                raise Refused('Archive has links, special files or too many entries')
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if member.size < 0 or member.size > MAX_FILE:
                raise Refused('Generated file exceeds size limit', 413)
            total += member.size
            if total > MAX_WORKSPACE:
                raise Refused('Generated workspace exceeds quota', 413)
            source = archive.extractfile(member)
            if source is None:
                raise Refused('Invalid file in workspace archive')
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with target.open('xb') as output:
                remaining = member.size
                while remaining:
                    data = source.read(min(65536, remaining))
                    if not data:
                        raise Refused('Truncated workspace archive')
                    output.write(data)
                    remaining -= len(data)
    inventory(destination)


@contextlib.contextmanager
def workspace_archive(root: Path):
    """Normalize validated host files for Docker's archive-preserving copy.

    The host store is private and locked by the caller. Never delegate traversal
    or ownership decisions to docker cp, and never include links in this tar.
    """
    files, _ = inventory(root)
    directories = []
    for directory, dirs, _ in os.walk(root, followlinks=False):
        for name in dirs:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            fd = secure_fd(root, relative, directory=True)
            os.close(fd)
            directories.append(relative)
            if len(directories) + len(files) > MAX_FILES * 2:
                raise Refused('Workspace has too many archive entries', 413)
    with tempfile.TemporaryFile() as stream:
        with tarfile.open(fileobj=stream, mode='w') as archive:
            # This root member is authored by the service, not a client path.
            for relative in ['.', *sorted(directories)]:
                member = tarfile.TarInfo(relative)
                member.type = tarfile.DIRTYPE
                member.uid = member.gid = 65532
                member.mode = 0o700
                archive.addfile(member)
            for relative, size in files:
                with os.fdopen(secure_fd(root, relative), 'rb') as source:
                    info = os.fstat(source.fileno())
                    if info.st_nlink != 1 or info.st_size != size:
                        raise Refused('Workspace changed while preparing its archive')
                    member = tarfile.TarInfo(relative)
                    member.uid = member.gid = 65532
                    member.mode = 0o600
                    member.size = size
                    archive.addfile(member, source)
        stream.seek(0)
        yield stream


@dataclasses.dataclass(frozen=True)
class Config:
    root: Path
    toolkit: Path
    image: str
    version: str
    source_url: str
    token: str
    docker: str = '/usr/local/bin/docker'
    docker_socket: str = '/var/run/docker.sock'
    timeout: int = 120
    create_timeout: int = 30
    max_calls: int = 100
    max_workspaces: int = 16
    ttl: int = 86400
    parallel: int = 2

    def validate(self):
        if not re.fullmatch(r'(?:[A-Za-z0-9./_:-]+@)?sha256:[a-f0-9]{64}', self.image):
            raise ValueError('NOVA_ENGINE_IMAGE must be an immutable repository@sha256 digest or sha256 image ID')
        if not re.fullmatch(r'[a-f0-9]{40}', self.version):
            raise ValueError('NOVA_ENGINE_VERSION must be the public source commit SHA')
        url = urllib.parse.urlsplit(self.source_url)
        if url.scheme != 'https' or url.netloc != 'github.com' or url.query or url.fragment or not re.fullmatch(r'/[\w.-]+/[\w.-]+/(?:tree|commit)/' + self.version, url.path):
            raise ValueError('NOVA_ENGINE_SOURCE_URL must identify the exact public GitHub source commit')
        if len(self.token) < 32 or self.token.isspace():
            raise ValueError('NOVA_ENGINE_TOKEN must contain at least 32 characters')
        if not self.root.is_absolute() or not self.toolkit.is_absolute() or self.root == self.toolkit or self.toolkit in self.root.parents:
            raise ValueError('Use an absolute private workspace directory outside the public toolkit')
        if not 1 <= self.timeout <= 600 or not 1 <= self.max_calls <= 500 or not 1 <= self.max_workspaces <= 128 or not 1 <= self.parallel <= 8 or not 60 <= self.ttl <= 604800:
            raise ValueError('Runtime quotas are outside their supported ranges')
        if not 1 <= self.create_timeout <= min(120, self.timeout):
            raise ValueError('NOVA_ENGINE_CREATE_TIMEOUT must be 1–120 seconds and cannot exceed NOVA_ENGINE_TIMEOUT')
        if not Path(self.docker).is_absolute() or not Path(self.docker_socket).is_absolute():
            raise ValueError('Docker executable and local socket must be absolute paths')


class DockerRunner:
    def __init__(self, config: Config):
        self.config = config
        self.config_dir = config.root / 'docker-config'
        self.config_dir.mkdir(mode=0o700, exist_ok=True)
        atomic_json(self.config_dir / 'config.json', {})
        self.environment = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': str(self.config_dir), 'LANG': 'C.UTF-8'}
        self.slots = threading.BoundedSemaphore(config.parallel)
        self.instance = hashlib.sha256(str(config.root).encode()).hexdigest()[:16]
        self.active = set()
        self.orphans = set()
        self.uncertain_starts = set()
        self.volumes = {}
        self.uncertain_volumes = set()
        self.reservation_dir = config.root / 'reservations'
        self.reservation_dir.mkdir(mode=0o700, exist_ok=True)
        self.reservations = {}
        self.active_lock = threading.Lock()
        self.drained = threading.Condition(self.active_lock)
        self.running = {}
        self.closing = False

    def record_resource(self, name, resource, state):
        """Fsync intent before a daemon RPC; a crash must not forget late creates."""
        with self.active_lock:
            record = dict(self.reservations.get(name, {
                'version': 1, 'name': name, 'container': 'none', 'volume': 'none',
            }))
            record[resource] = state
            atomic_json(self.reservation_dir / (name + '.json'), record)
            self.reservations[name] = record
            uncertain = self.uncertain_starts if resource == 'container' else self.uncertain_volumes
            if state == 'pending':
                uncertain.add(name)
            else:
                uncertain.discard(name)
            if resource == 'volume':
                if state in ('pending', 'confirmed'):
                    self.volumes[name] = 'nova-work-' + name.removeprefix('nova-job-')
                else:
                    self.volumes.pop(name, None)

    def recover_reservations(self):
        # Called only after the service store lock is held. Atomic-write scratch
        # files never precede an issued RPC and do not replace the stable marker.
        for path in sorted(self.reservation_dir.iterdir()):
            if path.name.startswith('.') and path.name.endswith('.tmp'):
                continue
            if not re.fullmatch(r'nova-job-[a-f0-9]{32}\.json', path.name):
                raise ValueError('Unexpected file in Docker reservation journal')
            record = json.loads(read_bytes(self.reservation_dir, path.name, 4096))
            if (not isinstance(record, dict) or set(record) != {'version', 'name', 'container', 'volume'}
                    or record['version'] != 1 or record['name'] != path.stem
                    or record['container'] not in ('none', 'pending', 'confirmed', 'removed')
                    or record['volume'] not in ('none', 'pending', 'confirmed', 'removed')):
                raise ValueError('Invalid Docker reservation journal entry')
            name = record['name']
            with self.active_lock:
                if name in self.active:
                    continue
                if not self.slots.acquire(blocking=False):
                    raise ValueError('Unresolved Docker reservations exceed the configured parallel limit; recover the dedicated host first')
                self.active.add(name)
                self.orphans.add(name)
                self.reservations[name] = record
                if record['container'] == 'pending':
                    self.uncertain_starts.add(name)
                if record['volume'] == 'pending':
                    self.uncertain_volumes.add(name)
                if record['volume'] in ('pending', 'confirmed'):
                    self.volumes[name] = 'nova-work-' + name.removeprefix('nova-job-')
        self.reap_orphans()

    def preflight(self):
        info = json.loads(self.checked(['image', 'inspect', self.config.image], limit=128 * 1024))[0]
        labels = info.get('Config', {}).get('Labels', {}) or {}
        repository = self.config.source_url.rsplit('/', 2)[0]
        if labels.get('org.opencontainers.image.revision') != self.config.version or labels.get('org.opencontainers.image.source') != repository:
            raise ValueError('Runtime image labels must match the exact public source release')
        self.recover_reservations()
        # Reclaim this dedicated service instance's leftovers after a host crash.
        names = self.checked(['ps', '--all', '--filter', 'label=org.secondbrick.instance=' + self.instance, '--format', '{{.Names}}']).decode().splitlines()
        for name in names:
            if re.fullmatch(r'nova-job-[a-f0-9]{32}', name) and name not in self.active:
                self.checked(['rm', '--force', name])
        volumes = self.checked(['volume', 'ls', '--filter', 'label=org.secondbrick.instance=' + self.instance,
                                '--filter', 'label=org.secondbrick.engine=isolated-workspace', '--format', '{{.Name}}']).decode().splitlines()
        for volume in volumes:
            if re.fullmatch(r'nova-work-[a-f0-9]{32}', volume) and volume not in self.volumes.values():
                self.checked(['volume', 'rm', volume])

    def command(self, args):
        return [self.config.docker, '--host', 'unix://' + self.config.docker_socket, '--config', str(self.config_dir), *args]

    def process(self, args, *, limit=MAX_OUTPUT, timeout=30, cancelled=None, stdin=None):
        if cancelled is not None and cancelled.is_set():
            raise Refused('Engine command cancelled or timed out', 504)
        if stdin is not None and not stat.S_ISREG(os.fstat(stdin.fileno()).st_mode):
            raise Refused('Container input must be a prepared ordinary file')
        process = subprocess.Popen(self.command(args), stdin=stdin if stdin is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, env=self.environment, start_new_session=True)
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() > deadline or cancelled is not None and cancelled.is_set():
                        raise Refused('Engine command cancelled or timed out', 504)
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fd)
                        else:
                            output.extend(chunk)
                            if len(output) > limit:
                                raise Refused('Engine command output exceeded its limit', 413)
            while process.poll() is None:
                if time.monotonic() > deadline or cancelled is not None and cancelled.is_set():
                    raise Refused('Engine command cancelled or timed out', 504)
                time.sleep(0.05)
            return process.returncode, bytes(output)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            process.stdout.close()

    def checked(self, args, **kwargs):
        code, result = self.process(args, **kwargs)
        if code:
            raise Refused('Container operation failed: ' + result.decode('utf-8', 'replace')[:2000], 502)
        return result

    def reap_orphans(self):
        with self.active_lock:
            names = list(self.orphans)
        for name in names:
            self.remove(name)

    def healthy(self):
        with self.active_lock:
            return not self.closing and not self.orphans

    def run(self, workspace: Path, body, cancelled):
        with self.active_lock:
            if self.closing:
                raise Refused('Engine is shutting down', 503)
        self.reap_orphans()
        if not self.healthy():
            raise Refused('Container cleanup is unavailable; new execution is paused', 503)
        if not self.slots.acquire(timeout=5):
            raise Refused('Engine is at its parallel command limit', 429)
        name = 'nova-job-' + uuid.uuid4().hex
        with self.active_lock:
            # close() may have started while this request waited for capacity.
            # Register creation and its cancellation under the same lock, before
            # any Docker command can begin.
            if self.closing:
                self.slots.release()
                raise Refused('Engine is shutting down', 503)
            self.active.add(name)
            self.running[name] = cancelled
        timer = threading.Timer(self.config.timeout, cancelled.set)
        timer.daemon = True
        try:
            timer.start()
            if cancelled.is_set():
                raise Refused('Engine command cancelled or timed out', 504)
            volume = 'nova-work-' + name.removeprefix('nova-job-')
            self.record_resource(name, 'volume', 'pending')
            # Docker's local tmpfs driver supports archive copy with a read-only
            # container root. It is a bounded memory volume, never a host bind.
            self.checked(['volume', 'create', '--driver', 'local',
                          '--label', 'org.secondbrick.engine=isolated-workspace',
                          '--label', 'org.secondbrick.instance=' + self.instance,
                          '--opt', 'type=tmpfs', '--opt', 'device=tmpfs',
                          '--opt', 'o=size=64m,nosuid,nodev,noexec,mode=0700,uid=65532,gid=65532', volume],
                         timeout=VOLUME_CREATE_TIMEOUT)
            self.record_resource(name, 'volume', 'confirmed')
            if cancelled.is_set():
                raise Refused('Engine command cancelled or timed out', 504)
            self.record_resource(name, 'container', 'pending')
            # This control phase starts only an inert PID 1. Let its bounded
            # Docker request finish even if the caller cancels; killing the CLI
            # does not guarantee cancellation of daemon-side creation. Author
            # execution below still observes cancellation before it can start.
            self.checked([
                'run', '--detach', '--rm', '--pull=never', '--name', name,
                '--label', 'org.secondbrick.engine=isolated-job',
                '--label', 'org.secondbrick.instance=' + self.instance, '--network=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user', '65532:65532',
                '--pids-limit=128', '--memory=1g', '--memory-swap=1g', '--cpus=2',
                '--ulimit', 'nofile=256:256', '--ulimit', f'fsize={RUNTIME_FILE_LIMIT}:{RUNTIME_FILE_LIMIT}',
                '--ulimit', 'core=0:0', '--log-driver=none',
                '--mount', f'type=volume,source={volume},target=/job,volume-nocopy',
                '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777',
                '--tmpfs', '/opt/nova/.cache:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=65532,gid=65532',
                '--workdir', '/job', '--env', 'HOME=/tmp', '--env', 'TMPDIR=/tmp',
                '--env', 'PYTHONPATH=/opt/nova', '--env', 'PYTHONDONTWRITEBYTECODE=1',
                '--env', 'PYTHONNOUSERSITE=1', '--env', 'LDRAW_DIR=/opt/ldraw',
                '--env', 'QT_QPA_PLATFORM=xcb', '--env', 'LIBGL_ALWAYS_SOFTWARE=1',
                '--env', 'XDG_CACHE_HOME=/tmp/cache', '--env', 'LANG=C.UTF-8',
                '--env', 'OMP_NUM_THREADS=2', '--env', 'OPENBLAS_NUM_THREADS=2',
                '--env', 'MKL_NUM_THREADS=2', '--env', 'LP_NUM_THREADS=2',
                '--entrypoint', '/opt/nova/.venv/bin/python', self.config.image,
                '-I', '-c', 'import time; time.sleep(900)',
            ], timeout=self.config.create_timeout)
            self.record_resource(name, 'container', 'confirmed')
            if cancelled.is_set():
                raise Refused('Engine command cancelled or timed out', 504)
            with workspace_archive(workspace) as staged_input:
                self.checked(['cp', '--archive', '-', name + ':/job'], stdin=staged_input, cancelled=cancelled)
            # The image contains a prebuilt public-data index. Give each command
            # its own writable copy, owned by the unprivileged execution UID.
            self.checked(['exec', name, '/opt/nova/.venv/bin/python', '-I', '-c',
                          "import shutil; shutil.copytree('/opt/nova-cache-seed', '/opt/nova/.cache', symlinks=True, dirs_exist_ok=True)"],
                         cancelled=cancelled)
            if body['kind'] == 'cli':
                command = ['/opt/nova/.venv/bin/python', '-m', 'ldraw_tools.cli', *body['args']]
            else:
                command = ['/opt/nova/.venv/bin/python', '/job/' + body['path'], *body['args']]
            code, output = self.process(['exec', '--workdir', '/job', name, *command], limit=MAX_OUTPUT, timeout=self.config.timeout, cancelled=cancelled)
            # Freeze author children too, so artifacts cannot change during export.
            self.checked(['pause', name], cancelled=cancelled)
            # Import into a new generation. A crash never publishes half an archive.
            archive = self.checked(['cp', name + ':/job/.', '-'], limit=MAX_WORKSPACE + 4 * MiB, cancelled=cancelled)
            staging = Path(tempfile.mkdtemp(prefix='generation-', dir=workspace.parent))
            try:
                import_archive(io.BytesIO(archive), staging)
                return code, output.decode('utf-8', 'replace'), staging
            except BaseException:
                shutil.rmtree(staging)
                raise
        finally:
            timer.cancel()
            # Failed cleanup keeps the slot reserved. Never admit more author
            # processes while an unaccounted container might still be alive.
            try:
                self.remove(name)
            finally:
                with self.drained:
                    self.running.pop(name, None)
                    self.drained.notify_all()

    def remove(self, name, *, timeout=CLEANUP_TIMEOUT):
        deadline = time.monotonic() + timeout
        try:
            with self.active_lock:
                record = self.reservations.get(name)
            if record is None or record['container'] in ('pending', 'confirmed'):
                code, output = self.process(['rm', '--force', name], limit=4096, timeout=min(10, timeout))
                if code and b'No such container' not in output:
                    raise RuntimeError('Container removal failed')
                with self.active_lock:
                    if code and name in self.uncertain_starts:
                        # This RPC may still be creating in Docker, including
                        # after a service restart. Absence is not reclamation.
                        self.orphans.add(name)
                        return False
                if record is not None:
                    self.record_resource(name, 'container', 'removed')
                else:
                    with self.active_lock:
                        self.uncertain_starts.discard(name)
            with self.active_lock:
                volume = self.volumes.get(name)
            # Never remove the volume until the container is confirmed gone:
            # an unresolved late create may still mount that volume.
            if volume is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError('Workspace volume cleanup timed out')
                code, output = self.process(['volume', 'rm', volume], limit=4096, timeout=min(10, remaining))
                if code and b'no such volume' not in output.lower():
                    raise RuntimeError('Workspace volume removal failed')
                with self.active_lock:
                    if code and name in self.uncertain_volumes:
                        self.orphans.add(name)
                        return False
                if record is not None:
                    self.record_resource(name, 'volume', 'removed')
            # Resource deletion and journal deletion must be durable before
            # releasing a slot. A crash at an ambiguous point fails closed.
            with self.active_lock:
                (self.reservation_dir / (name + '.json')).unlink(missing_ok=True)
                fd = os.open(self.reservation_dir, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                self.reservations.pop(name, None)
        except Exception:
            with self.active_lock:
                self.orphans.add(name)
            return False
        with self.active_lock:
            self.orphans.discard(name)
            self.uncertain_starts.discard(name)
            self.uncertain_volumes.discard(name)
            self.volumes.pop(name, None)
            if name in self.active:
                self.active.remove(name)
                self.slots.release()
        return True

    def close(self, *, timeout=None):
        if timeout is None:
            timeout = VOLUME_CREATE_TIMEOUT + self.config.create_timeout + CLEANUP_TIMEOUT + 5
        deadline = time.monotonic() + timeout
        with self.drained:
            self.closing = True
            for cancelled in self.running.values():
                cancelled.set()
            # Let each run cancel its Docker client, finish any pending creation,
            # then remove its own container in finally. Removing a name before
            # creation has returned can falsely report "No such container".
            while self.running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.drained.wait(remaining)
            names = list(self.active)
        for name in names:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self.remove(name, timeout=min(CLEANUP_TIMEOUT, remaining))
        with self.active_lock:
            return not self.active


class Engine:
    def __init__(self, config: Config, runner=None):
        config.validate()
        self.config = config
        config.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if config.root.is_symlink():
            raise ValueError('Workspace root cannot be a symlink')
        os.chmod(config.root, 0o700)
        self.workspaces = config.root / 'workspaces'
        self.workspaces.mkdir(mode=0o700, exist_ok=True)
        self.runner = runner if runner is not None else DockerRunner(config)
        self.guard = threading.Lock()

    def source(self):
        return {'engineVersion': self.config.version, 'sourceUrl': self.config.source_url,
                'license': 'AGPL-3.0-only', 'image': self.config.image}

    @contextlib.contextmanager
    def locked(self, id, *, allow_inactive=False, cancelled=None):
        if not isinstance(id, str) or not ID.fullmatch(id):
            raise Refused('Invalid workspace ID', 404)
        directory = self.workspaces / id
        try:
            fd = os.open(directory / 'lock', os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            raise Refused('Workspace not found', 404)
        try:
            while True:
                if cancelled is not None and cancelled.is_set():
                    raise Refused('Engine call cancelled', 499)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(0.05)
            try:
                metadata = json.loads((directory / 'meta.json').read_text())
            except FileNotFoundError:
                raise Refused('Workspace not found', 404)
            if not allow_inactive and metadata['expiresAt'] <= time.time():
                raise Refused('Workspace expired', 410)
            if not allow_inactive and (metadata['engineVersion'] != self.config.version or metadata['image'] != self.config.image):
                raise Refused('Workspace belongs to a different engine release', 409)
            yield directory, metadata
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def cleanup(self):
        for directory in self.workspaces.iterdir():
            if not ID.fullmatch(directory.name):
                continue
            try:
                fd = os.open(directory / 'lock', os.O_RDWR | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                metadata = json.loads((directory / 'meta.json').read_text())
                if metadata['expiresAt'] <= time.time():
                    shutil.rmtree(directory)
            except (BlockingIOError, FileNotFoundError):
                pass
            finally:
                os.close(fd)

    def create(self, body):
        if not isinstance(body, dict) or set(body) - {'files'} or not isinstance(body.get('files', []), list):
            raise Refused('Expected {files:[{path,text}]}')
        files = body.get('files', [])
        if len(files) > MAX_FILES:
            raise Refused('Too many initial files', 413)
        with self.guard:
            self.cleanup()
            if len(list(self.workspaces.iterdir())) >= self.config.max_workspaces:
                raise Refused('Workspace limit reached; delete unused workspaces', 429)
            id = uuid.uuid4().hex
            directory = self.workspaces / id
            directory.mkdir(mode=0o700)
            try:
                (directory / 'lock').touch(mode=0o600)
                (directory / 'calls').mkdir(mode=0o700)
                generation = directory / 'generation-initial'
                generation.mkdir(mode=0o700)
                names = set()
                for entry in files:
                    if not isinstance(entry, dict) or set(entry) != {'path', 'text'} or not isinstance(entry['text'], str):
                        raise Refused('Each initial file must contain path and text')
                    path_parts(entry['path'])
                    if entry['path'] in names:
                        raise Refused('Duplicate initial file')
                    names.add(entry['path'])
                    put_file(generation, entry['path'], entry['text'].encode())
                meta = {**self.source(), 'id': id, 'generation': generation.name, 'calls': 0,
                        'expiresAt': time.time() + self.config.ttl}
                atomic_json(directory / 'meta.json', meta)
            except BaseException:
                shutil.rmtree(directory)
                raise
        return {'id': id, **self.source()}

    def delete(self, id):
        with self.locked(id, allow_inactive=True) as (directory, _):
            shutil.rmtree(directory)
        return {'text': 'Workspace deleted.'}

    def images(self, workspace, paths):
        result = []
        for path in paths:
            content = read_bytes(workspace, path, MAX_IMAGE)
            if content.startswith(b'\x89PNG\r\n\x1a\n'):
                mime = 'image/png'
            elif content.startswith(b'\xff\xd8\xff'):
                mime = 'image/jpeg'
            else:
                raise Refused('Only PNG and JPEG image files may be returned')
            result.append('data:' + mime + ';base64,' + base64.b64encode(content).decode('ascii'))
        return result

    def validate_call(self, body):
        if not isinstance(body, dict) or set(body) - {'idempotencyKey', 'action', 'path', 'text', 'source', 'kind', 'args', 'imagePaths'}:
            raise Refused('Invalid call fields')
        if not isinstance(body.get('idempotencyKey'), str) or not KEY.fullmatch(body['idempotencyKey']):
            raise Refused('idempotencyKey must contain 1–96 letters, numbers, underscores or hyphens')
        action = body.get('action')
        if action not in ('write', 'read', 'list', 'run'):
            raise Refused('Unknown action')
        source = body.get('source', 'workspace')
        if source not in ('workspace', 'toolkit') or source == 'toolkit' and action not in ('read', 'list'):
            raise Refused('Toolkit source supports read/list only')
        images = body.get('imagePaths', [])
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise Refused('imagePaths must list at most four workspace images')
        for path in images:
            path_parts(path)
        if action in ('write', 'read', 'list'):
            path_parts(body.get('path', ''), root=action == 'list')
        if action == 'write' and not isinstance(body.get('text'), str):
            raise Refused('write requires text')
        if action == 'run':
            if body.get('kind') not in ('cli', 'python'):
                raise Refused('run requires kind cli or python')
            args = body.get('args', [])
            if not isinstance(args, list) or len(args) > MAX_ARGS or any(not isinstance(a, str) or len(a) > 4096 or '\x00' in a for a in args):
                raise Refused('args must be a bounded string array')
            if body['kind'] == 'python':
                path_parts(body.get('path'))
                if not body['path'].endswith('.py'):
                    raise Refused('Python commands require a workspace .py file')
        return {**body, 'source': source, 'args': body.get('args', []), 'imagePaths': images}

    def read_result(self, workspace, body):
        """Prepare a non-mutating read while the workspace lock is held."""
        path = body['path']
        source = self.config.toolkit if body['source'] == 'toolkit' else workspace
        if body['source'] == 'toolkit' and path_parts(path)[0] not in PUBLIC_ROOTS:
            raise Refused('Path is outside the public toolkit')
        if path.lower().endswith(('.png', '.jpg', '.jpeg')) and body['source'] == 'workspace':
            result = {'text': path, 'images': self.images(workspace, [path])}
        else:
            try:
                result = {'text': read_bytes(source, path, MAX_READ_TEXT).decode('utf-8')}
            except UnicodeDecodeError:
                raise Refused('File is not UTF-8 text; read PNG/JPEG images by their path')
        if body['imagePaths']:
            result['images'] = self.images(workspace, body['imagePaths'])
        return result

    def call(self, id, raw, cancelled=None):
        body = self.validate_call(raw)
        cancelled = cancelled or threading.Event()
        digest = hashlib.sha256(canonical(raw)).hexdigest()
        with self.locked(id, cancelled=cancelled) as (directory, meta):
            record_path = directory / 'calls' / (body['idempotencyKey'] + '.json')
            if record_path.exists():
                record = json.loads(record_path.read_text())
                if record['hash'] != digest:
                    raise Refused('Idempotency key was already used for a different request', 409)
                if record['state'] != 'done':
                    raise Refused('Previous call was interrupted; it will not be executed again. Use a new key after inspecting the workspace.', 409)
                if record.get('error'):
                    raise Refused(record['error'], record['status'])
                return record['result']
            if meta['calls'] >= self.config.max_calls:
                raise Refused('Workspace call limit reached', 429)
            cache_bytes = sum(p.stat().st_size for p in (directory / 'calls').iterdir())
            workspace = directory / meta['generation']
            response_limit = MAX_READ_RESPONSE if body['action'] == 'read' else MAX_RESPONSE
            prepared_read = read_error = None
            if body['action'] == 'read':
                # Read-only preparation has no external effects. Reserve its
                # actual encoding so a large MPD cannot crowd out tiny authored
                # instructions merely because both share a maximum read limit.
                try:
                    prepared_read = self.read_result(workspace, body)
                    encoded_size = len(canonical(prepared_read))
                    if encoded_size > response_limit:
                        raise Refused('Encoded response exceeds its limit', 413)
                except Refused as exc:
                    read_error = exc
                    encoded_size = len(canonical({'error': str(exc), 'status': exc.status}))
                reserve = encoded_size + 4096
            else:
                # Commands and writes retain their pre-execution reservation.
                reserve = response_limit + 4096
            if cache_bytes + reserve > MAX_CACHE:
                raise Refused('Workspace response cache quota reached', 429)
            meta['calls'] += 1
            atomic_json(directory / 'meta.json', meta)
            atomic_json(record_path, {'hash': digest, 'state': 'started'})
            # Recover stale unpublished generations left by an interrupted host
            # process; refuse further execution if they cannot be reclaimed.
            for previous in directory.glob('generation-*'):
                if previous != workspace:
                    try:
                        shutil.rmtree(previous)
                    except OSError:
                        raise Refused('Workspace cleanup failed; delete the workspace before continuing', 503)
            staging = None
            try:
                action = body['action']
                source = self.config.toolkit if body['source'] == 'toolkit' else workspace
                path = body.get('path', '')
                if body['source'] == 'toolkit':
                    parts = path_parts(path, root=action == 'list')
                    if parts and parts[0] not in PUBLIC_ROOTS:
                        raise Refused('Path is outside the public toolkit')
                if action == 'write':
                    content = body['text'].encode('utf-8')
                    if len(content) > MAX_TEXT:
                        raise Refused('Write text exceeds its limit', 413)
                    put_file(workspace, path, content)
                    result = {'text': 'Wrote ' + path}
                elif action == 'read':
                    if read_error is not None:
                        raise read_error
                    result = prepared_read
                elif action == 'list':
                    fd = secure_fd(source, path, directory=True)
                    try:
                        entries = []
                        for name in sorted(os.listdir(fd)):
                            if name.startswith('.') or body['source'] == 'toolkit' and path in ('', '.') and name not in PUBLIC_ROOTS:
                                continue
                            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                            if stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                                entries.append(name + ('/' if stat.S_ISDIR(info.st_mode) else ''))
                            if len(entries) > MAX_FILES:
                                raise Refused('Directory exceeds listing limit', 413)
                        result = {'text': '\n'.join(entries)}
                    finally:
                        os.close(fd)
                else:
                    if body['kind'] == 'python':
                        read_bytes(workspace, path)
                    code, text, staging = self.runner.run(workspace, body, cancelled)
                    result = {'text': f'Exit code: {code}\n{text}'}
                text_limit = MAX_READ_TEXT if action == 'read' else MAX_TEXT
                if len(result['text'].encode('utf-8')) > text_limit:
                    raise Refused('Response text exceeds its limit', 413)
                active = staging or workspace
                if body['imagePaths'] and action != 'read':
                    result['images'] = self.images(active, body['imagePaths'])
                if len(canonical(result)) > response_limit:
                    raise Refused('Encoded response exceeds its limit', 413)
                if staging is not None:
                    meta['generation'] = staging.name
                    atomic_json(directory / 'meta.json', meta)
                    staging = None  # Published: never remove it during error cleanup.
                    shutil.rmtree(workspace, ignore_errors=True)
                atomic_json(record_path, {'hash': digest, 'state': 'done', 'result': result})
                return result
            except Refused as exc:
                atomic_json(record_path, {'hash': digest, 'state': 'done', 'error': str(exc), 'status': exc.status})
                raise
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'NovaEngine/1'
    protocol_version = 'HTTP/1.0'

    def log_message(self, format, *args):
        # No URL, body, authorization headers or model contents in access logs.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, status, value):
        body = canonical(value)
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        engine = self.server.engine
        if self.command == 'GET' and self.path in ('/health', '/source'):
            healthy = self.path == '/source' or not hasattr(engine.runner, 'healthy') or engine.runner.healthy()
            self.reply(200 if healthy else 503, {'ok': healthy, **engine.source()})
            return
        auth = self.headers.get('Authorization', '')
        if not hmac.compare_digest(auth, 'Bearer ' + engine.config.token):
            raise Refused('Unauthorized', 401)
        if self.command == 'GET' and self.path == '/v1/ready':
            healthy = not hasattr(engine.runner, 'healthy') or engine.runner.healthy()
            self.reply(200 if healthy else 503, {'ok': healthy, **engine.source()})
            return
        body = None
        if self.command == 'POST':
            if self.headers.get('Transfer-Encoding') or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Refused('Use application/json with Content-Length')
            try:
                size = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                raise Refused('Invalid Content-Length')
            if size < 0 or size > MAX_REQUEST:
                raise Refused('Request exceeds size limit', 413)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise Refused('Incomplete request body')
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                raise Refused('Invalid JSON')
        if self.command == 'POST' and self.path == '/v1/workspaces':
            self.reply(201, engine.create(body))
            return
        match = re.fullmatch(r'/v1/workspaces/([a-f0-9]{32})(/calls)?', self.path)
        if match:
            id, calls = match.groups()
            if self.command == 'DELETE' and not calls:
                self.reply(200, engine.delete(id))
                return
            if self.command == 'POST' and calls:
                cancelled = threading.Event()
                finished = threading.Event()
                # Peer disconnect is observed while a command is running.
                def monitor():
                    import socket
                    while not finished.wait(0.2):
                        try:
                            import select
                            if select.select([self.connection], [], [], 0)[0]:
                                if not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                                    cancelled.set()
                                    return
                        except (OSError, ValueError):
                            cancelled.set()
                            return
                thread = threading.Thread(target=monitor, daemon=True)
                thread.start()
                try:
                    self.reply(200, engine.call(id, body, cancelled))
                finally:
                    finished.set()
                    thread.join(timeout=1)
                return
        raise Refused('Endpoint not found', 404)

    def handle_request(self):
        try:
            self.dispatch()
        except Refused as exc:
            self.reply(exc.status, {'error': str(exc)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            # An interrupted idempotency reservation deliberately remains failed
            # closed instead of repeating an operation of uncertain outcome.
            self.reply(500, {'error': 'Engine operation failed; inspect workspace with a new call key'})

    do_GET = do_POST = do_DELETE = handle_request


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, engine):
        self.engine = engine
        self.connections = threading.BoundedSemaphore(32)
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


def main():
    config = Config(
        root=Path(os.environ['NOVA_ENGINE_WORKSPACES']), toolkit=Path(__file__).resolve().parents[2],
        image=os.environ['NOVA_ENGINE_IMAGE'], version=os.environ['NOVA_ENGINE_VERSION'],
        source_url=os.environ['NOVA_ENGINE_SOURCE_URL'], token=os.environ['NOVA_ENGINE_TOKEN'],
        docker=os.environ.get('NOVA_ENGINE_DOCKER', '/usr/local/bin/docker'),
        docker_socket=os.environ.get('NOVA_ENGINE_DOCKER_SOCKET', '/var/run/docker.sock'),
        timeout=int(os.environ.get('NOVA_ENGINE_TIMEOUT', '120')),
        create_timeout=int(os.environ.get('NOVA_ENGINE_CREATE_TIMEOUT', '30')),
    )
    engine = Engine(config)
    # One orchestrator owns a store and its container labels at a time.
    service_lock = os.open(config.root / 'service.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(service_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    engine.runner.preflight()
    server = Server((os.environ.get('NOVA_ENGINE_BIND', '127.0.0.1'), int(os.environ.get('NOVA_ENGINE_PORT', '8788'))), engine)
    maintenance_stop = threading.Event()
    def maintain():
        while not maintenance_stop.wait(15):
            engine.runner.reap_orphans()
            with engine.guard:
                engine.cleanup()
    maintenance = threading.Thread(target=maintain, daemon=True)
    maintenance.start()
    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        maintenance_stop.set()
        cleaned = engine.runner.close()
        maintenance.join(timeout=15)
        server.server_close()
        os.close(service_lock)
        if not cleaned:
            raise RuntimeError('Engine shutdown could not confirm container cleanup; recover the dedicated Docker host before restarting author execution')


if __name__ == '__main__':
    main()
