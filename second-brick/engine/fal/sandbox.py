# SPDX-License-Identifier: AGPL-3.0-only
"""Credential-free Nova commands in bounded Linux namespaces on fal."""
from __future__ import annotations

import base64
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

MAX_WIRE = 48 * 1024 * 1024


def toolkit_server():
    name = "second_brick_nova_server"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, "/opt/nova/second-brick/engine/server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def unpack(value: str) -> bytes:
    if len(value) > MAX_WIRE:
        raise ValueError("Workspace transfer exceeds its limit")
    compressed = base64.b64decode(value, validate=True)
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as source:
        data = source.read(36 * 1024 * 1024 + 1)
    if len(data) > 36 * 1024 * 1024:
        raise ValueError("Expanded workspace exceeds its limit")
    return data


def pack(data: bytes) -> str:
    result = base64.b64encode(gzip.compress(data, compresslevel=3)).decode()
    if len(result) > MAX_WIRE:
        raise ValueError("Workspace result exceeds its limit")
    return result


def capture(command, *, data=None, timeout=120, limit=64 * 1024, env=None, cancelled=None):
    """Bound output while it is produced, and kill the complete process group."""
    with subprocess.Popen(command, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                          start_new_session=True) as process:
        selector = selectors.DefaultSelector()
        output = bytearray()
        deadline = time.monotonic() + timeout
        if data is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
        selector.register(process.stdout, selectors.EVENT_READ)
        sent = 0
        try:
            while selector.get_map():
                if cancelled is not None and cancelled.is_set():
                    raise RuntimeError("Sandbox command was cancelled")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Sandbox command exceeded its time limit")
                for key, event in selector.select(min(0.2, deadline - time.monotonic())):
                    if event & selectors.EVENT_WRITE:
                        try:
                            sent += os.write(key.fileobj.fileno(), data[sent:sent + 65536])
                        except BrokenPipeError:
                            sent = len(data)
                        if sent == len(data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > limit:
                                raise ValueError("Sandbox output exceeded its limit")
            return process.wait(timeout=max(0.01, deadline - time.monotonic())), bytes(output)
        finally:
            selector.close()
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()


def sandbox_command(*, gpu=False):
    # setpriv makes host UID 65532 subject to RLIMIT_NPROC too. /data, /app,
    # service state, sockets and the parent PID namespace are never mounted.
    args = ["setpriv", "--reuid=65532", "--regid=65532", "--clear-groups",
            "prlimit", "--cpu=120", "--fsize=134217728", "--nofile=256", "--nproc=128", "--core=0"]
    if not gpu:
        # Mesa reserves substantially more virtual address space than its RSS.
        # A 1 GiB address-space cap is not equivalent to Docker's 1 GiB memory
        # cgroup and prevents rendering on four-core CPU runners.
        args.append("--as=4294967296")
    # fal's network implementation cannot configure a fresh loopback device.
    # Retain the network namespace but deny non-Unix sockets with inherited
    # seccomp before starting any author program. No network FD is inherited.
    args += ["--", "bwrap", "--unshare-all", "--unshare-user", "--share-net", "--disable-userns", "--die-with-parent", "--new-session",
             "--cap-drop", "ALL", "--clearenv"]
    for path in ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt/nova", "/opt/ldraw", "/opt/nova-cache-seed"]:
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    for path in ["/etc/fonts", "/etc/alternatives", "/etc/passwd", "/etc/group",
                 "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d"]:
        if Path(path).exists():
            args += ["--ro-bind", path, path]
    args += ["--ro-bind", str(Path(__file__).resolve()), "/opt/fal-worker/sandbox.py",
             "--proc", "/proc", "--dev", "/dev", "--size", "67108864", "--tmpfs", "/job",
             "--size", "134217728", "--tmpfs", "/tmp",
             "--size", "134217728", "--tmpfs", "/opt/nova/.cache", "--chdir", "/job"]
    if gpu:
        args += ["--ro-bind", "/opt/VirtualGL", "/opt/VirtualGL"]
        args += ["--ro-bind", "/opt/fal-worker/leocad-gpu", "/usr/bin/leocad"]
        for path in sorted(Path("/dev").glob("nvidia*")):
            args += ["--dev-bind", str(path), str(path)]
        if Path("/dev/dri").exists():
            args += ["--dev-bind", "/dev/dri", "/dev/dri"]
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
           "PYTHONPATH": "/opt/nova", "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "LDRAW_DIR": "/opt/ldraw", "QT_QPA_PLATFORM": "xcb", "LIBGL_ALWAYS_SOFTWARE": "0" if gpu else "1",
           "XDG_CACHE_HOME": "/tmp/cache", "LANG": "C.UTF-8", "OMP_NUM_THREADS": "2",
           "XDG_RUNTIME_DIR": "/tmp/runtime", "QT_X11_NO_MITSHM": "1",
           "OPENBLAS_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "LP_NUM_THREADS": "2"}
    for key, value in env.items():
        args += ["--setenv", key, value]
    args += ["/usr/local/bin/python", "/opt/fal-worker/sandbox.py", "--inside"]
    return args


def execute(payload, *, gpu=False, cancelled=None):
    # The parent has no provider secrets. The child receives only this request.
    encoded = json.dumps(payload).encode()
    if len(encoded) > MAX_WIRE + 65536:
        raise ValueError("Sandbox input exceeds its limit")
    code, output = capture(sandbox_command(gpu=gpu), data=encoded, timeout=135, limit=MAX_WIRE + 100000, cancelled=cancelled)
    if code:
        raise RuntimeError(f"Sandbox process failed ({code}): {output[-500:].decode('utf-8', 'replace')}")
    return json.loads(output)


def restrict_network():
    """Allow local X11 sockets; deny every other socket family, across exec."""
    import ctypes
    import errno
    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                          ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("Could not initialize sandbox socket filter")
    try:
        for name in (b"socket", b"socketpair"):
            number = lib.seccomp_syscall_resolve_name(name)
            rule = Comparison(0, 1, 1, 0)  # SCMP_CMP_NE, AF_UNIX
            if number < 0 or lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM, number, 1, ctypes.pointer(rule)):
                raise RuntimeError("Could not restrict sandbox sockets")
        # Prevent switching to another namespace after the filter is loaded.
        for name in (b"setns", b"unshare", b"socketcall", b"ptrace", b"process_vm_readv", b"process_vm_writev"):
            number = lib.seccomp_syscall_resolve_name(name)
            if number >= 0 and lib.seccomp_rule_add_array(context, 0x00050000 | errno.EPERM, number, 0, None):
                raise RuntimeError("Could not restrict sandbox namespace access")
        if lib.seccomp_load(context):
            raise RuntimeError("Could not load sandbox socket filter")
    finally:
        lib.seccomp_release(context)


def inside():
    import shutil
    server = toolkit_server()
    payload = json.loads(sys.stdin.buffer.read(MAX_WIRE + 65537))
    body = payload["body"]
    if body.get("kind") not in ("cli", "python"):
        raise ValueError("Unsupported command kind")
    args = body.get("args", [])
    if not isinstance(args, list) or len(args) > 64 or any(not isinstance(x, str) or len(x) > 2048 or "\0" in x for x in args):
        raise ValueError("Invalid command arguments")
    workspace = Path("/job")
    server.import_archive(io.BytesIO(unpack(payload["archive"])), workspace)
    shutil.copytree("/opt/nova-cache-seed", "/opt/nova/.cache", symlinks=True, dirs_exist_ok=True)
    Path("/tmp/runtime").mkdir(mode=0o700)
    Path("/tmp/.X11-unix").mkdir(mode=0o1777)
    restrict_network()
    if body["kind"] == "python":
        path = body.get("path", "")
        server.path_parts(path)
        server.read_bytes(workspace, path)
        command = ["/opt/nova/.venv/bin/python", "/job/" + path, *args]
    else:
        command = ["/opt/nova/.venv/bin/python", "-m", "ldraw_tools.cli", *args]
    began = time.monotonic()
    code, output = capture(command, timeout=120, env=dict(os.environ))
    # No author process may change artifacts while they are being exported.
    # This /proc is confined to the sandbox's PID namespace.
    for _ in range(20):
        children = [int(p.name) for p in Path("/proc").iterdir()
                    if p.name.isdigit() and int(p.name) not in (1, os.getpid())]
        live = []
        for pid in children:
            try:
                if Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z":
                    os.kill(pid, signal.SIGKILL)
                    live.append(pid)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if not live:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("Sandbox children did not terminate")
    with server.workspace_archive(workspace) as archive:
        result = pack(archive.read())
    print(json.dumps({"code": code, "output": output.decode("utf-8", "replace"), "archive": result,
                      "seconds": round(time.monotonic() - began, 3)}))


if __name__ == "__main__":
    if sys.argv[1:] != ["--inside"]:
        raise SystemExit("This entry point is only used inside the sandbox")
    inside()
