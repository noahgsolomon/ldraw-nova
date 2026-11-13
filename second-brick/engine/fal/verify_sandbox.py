# SPDX-License-Identifier: AGPL-3.0-only
"""Fixed verification of the actual sandbox and LeoCAD; no model/provider calls."""
import io
import tarfile

from sandbox import execute, pack, unpack


def run(script):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as target:
        value = script.encode()
        member = tarfile.TarInfo("verify.py")
        member.size, member.mode = len(value), 0o600
        target.addfile(member, io.BytesIO(value))
    result = execute({"archive": pack(archive.getvalue()),
                      "body": {"kind": "python", "path": "verify.py", "args": []}})
    if result["code"]:
        raise RuntimeError("Sandbox verification failed: " + result["output"][-2000:])
    with tarfile.open(fileobj=io.BytesIO(unpack(result["archive"]))) as returned:
        files = {member.name: member.size for member in returned.getmembers()}
    return result, files


def isolation_check():
    result, files = run('''import os, socket, subprocess, sys
from pathlib import Path
assert os.getuid() == 65532, os.getuid()
assert not Path('/data').exists()
assert not Path('/app').exists()
assert not any('KEY' in name or 'TOKEN' in name or 'SECRET' in name for name in os.environ)
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raise AssertionError('Network socket was allowed')
except PermissionError:
    pass
socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).close()
p = subprocess.run([sys.executable, '-c', 'import socket; socket.socket()'], capture_output=True)
assert p.returncode != 0 and b'Operation not permitted' in p.stderr
try:
    Path('/opt/nova/should-not-write').write_text('fail')
    raise AssertionError('Toolkit was writable')
except OSError:
    pass
subprocess.Popen([sys.executable, '-c', "import time; from pathlib import Path; time.sleep(2); Path('/job/escaped').write_text('fail')"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
print('uid, filesystem, credentials, network, inherited seccomp, immutable toolkit: passed')
''')
    if "escaped" in files:
        raise RuntimeError("Sandbox did not terminate a detached child")
    return {"isolation": "passed", "descendant_cleanup": "passed", "seconds": result["seconds"]}


def run_checks():
    report = isolation_check()
    result, files = run('''from pathlib import Path
import subprocess
model = '0 Renderer smoke\\n' + ''.join(f'1 4 {x*80} 0 0 1 0 0 0 1 0 0 0 1 3001.dat\\n' for x in range(10))
Path('verify.ldr').write_text(model)
subprocess.run(['/usr/bin/leocad', '-l', '/opt/ldraw', '-i', '/job/render.png', '-w', '512', '-h', '512', '--viewpoint', 'home', '/job/verify.ldr'], check=True)
assert Path('render.png').stat().st_size > 1000
print('LeoCAD rendered actual LDraw geometry')
''')
    if files.get("render.png", 0) < 1000:
        raise RuntimeError("LeoCAD did not produce an image")
    return {**report, "render": "passed", "render_seconds": result["seconds"], "image_bytes": files["render.png"]}
