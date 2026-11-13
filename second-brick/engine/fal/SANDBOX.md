# CPU namespace adapter for the pinned Nova toolkit

SPDX-License-Identifier: AGPL-3.0-only

These adapter sources extend the public Nova workspace server at toolkit commit
`c0473bae3cc26722cf650bd5360443117203cbb6`. They do not replace or update its
LDraw data, CLI, renderer, import validation or transactional workspace protocol.

`sandbox_engine.py` provides the runner interface accepted by
`second-brick/engine/server.py::Engine(config, runner=...)`. It admits at most
`config.parallel` commands; additional calls wait and observe cancellation.
`sandbox.py` runs each command in fresh user, mount, PID, IPC, UTS and cgroup
namespaces using bubblewrap. fal's network implementation cannot initialize a
new loopback device, so network access is denied by an inherited libseccomp
filter: only AF_UNIX sockets are permitted, for the local X11 renderer.

The execution UID is 65532, all capabilities are dropped, the environment is
cleared, and only public OS/toolkit/library paths are mounted read-only. The
controller, account `/data`, service sockets and parent processes are not visible.
Writable storage is tmpfs: 64 MiB for the workspace and 128 MiB each for the
temporary directory and seeded cache. Preserve the seed's public-library
symlinks when copying it. Each command has a 120-second execution deadline,
64 KiB output cap, 128-process UID limit, 1 GiB per-process address-space limit,
128 MiB per-file limit and 256-file-descriptor limit. The whole fal CPU machine
supplies the aggregate memory/CPU bound; this is not Docker's former separate
1 GiB command cgroup. Detached descendants are killed before exporting results.

The controller retains the original server's strict archive import/export,
workspace ownership, quota checks, idempotency and source pin. No application
keys belong in toolkit files, generated program inputs, or an image layer.

The runtime needs Linux with unprivileged user namespaces, Python 3.12,
bubblewrap, util-linux, libseccomp2, the pinned toolkit and its prepared cache at
`/opt/nova-cache-seed`, and the official library at `/opt/ldraw`. Install these
three Python files together in `/opt/fal-worker` and add it to the trusted
controller's import path. Use `verify_sandbox.run_checks()` to verify UID,
filesystem and credential isolation, blocked network sockets across exec,
read-only toolkit, detached-child cleanup and an actual LeoCAD PNG render.
The checks make no model/provider call.
