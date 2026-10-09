# Isolated Nova workspace engine

This AGPL-3.0-only service exposes the **complete fork toolkit**, rather than a
prompt imitation. The caller can read Nova's guides and source, search its local
reference database, inspect parts, write generators and plans, assemble submodels,
render models and part boards, and generate instructions. Network-dependent Jev
search is deliberately unavailable inside author execution. Use explicit offline
FTS there; a separate trusted catalog adapter may provide approved search results.
No model provider, application credentials or private app code belongs in this repo.

## Isolation and persistence

The Python standard-library server stores logical workspaces and starts a fresh
Docker container for each command. It never executes author Python on the host.
Containers have no network, credentials, Docker socket or host bind mounts. They
use a read-only root, non-root UID 65532, dropped capabilities, no-new-privileges,
2 CPU/1 GiB memory/128 PID limits, bounded file sizes, and a fixed immutable image.
Each `/job` uses a dedicated labeled Docker local-driver **64 MiB tmpfs volume**;
`/tmp` and Nova's cache are separate 128 MiB tmpfs
mounts. Thus a hostile generator cannot fill the host disk through a workspace.
Failure to remove either a container or its volume retains its concurrency slot,
makes health unhealthy, and is retried; execution pauses until cleanup succeeds.
Startup also reclaims both types of labeled leftovers for this service instance.
Creation intent is fsynced in a private reservation journal before each Docker
create request. After a restart, pending requests keep their slot and unhealthy
status even when Docker currently lists no matching resource: a timed-out daemon
request may still complete later. Confirmed removal of both resources durably
clears the reservation before capacity is released.

The service archives the validated workspace with UID/GID 65532, private file and
directory modes, and no links, then uses Docker's archive-preserving copy into
`/job`. It invokes a fixed Python
entrypoint without a shell, then streams out the resulting files. It rejects
symlinks, hardlinks, devices, FIFOs, duplicate paths, hidden paths, path traversal,
files over 8 MiB, more than 2,048 files, and total file contents over 32 MiB.
A validated new generation is published atomically; interrupted exports leave the
old workspace intact. Command nonzero exit codes are returned to the author for
repair; a successful command is **not** evidence that its model is buildable.

The image includes an immutable public-data FTS cache seed. Before author code
runs, the unprivileged container copies it into its own writable cache tmpfs;
only links to the read-only official LDraw tree are preserved. During image
construction, the copied official top-level DAT timestamps and generated part
index timestamp are normalized to whole seconds before their cache signatures
are recorded. This keeps the seed reusable after Docker image export/import
without changing library contents, modes or native runtime cache invalidation.
The per-process
file-size ceiling is 128 MiB so Nova can use its approximately 93 MiB search
index. Workspace files remain limited to 8 MiB at both transfer boundaries; the
workspace and cache tmpfs limits independently bound disk-like allocations.

Per-workspace locks serialize actions. A persistent idempotency record prevents
re-execution after retries or crashes; an interrupted request returns 409 so the
caller can inspect state before issuing a new key. There are at most 100 calls,
64 MiB cached responses, 16 workspaces, and two running commands per server by
default. Workspaces expire after 24 hours and are reclaimed periodically and on workspace creation.
Public health/source endpoints identify the exact fork source and image; all
workspace endpoints require the server's bearer token. Put the loopback listener
behind a trusted TLS reverse proxy. Docker access is host-privileged: run this
orchestrator on a dedicated worker, not beside private application files/secrets.
A container sandbox does not defend against a kernel or Docker escape; keep the
host patched and use a dedicated worker or stronger VM boundary as appropriate.

## Build the runtime

Use an exported **public commit**, never a mixed/private checkout, as build context.
Supply the official LDraw library as a separate named build context; preserve its
license notices. The Dockerfile includes source, reference assets and pinned Python
packages from `uv.lock`, plus headless LeoCAD and PDF text extraction. Upstream commands that delegate to separately installed `mpd2glb.sh` or Jev require those optional external services/tools; they are not bundled or silently replaced. The base/apt layer is resolved at
build time; record the produced image digest and deploy that immutable digest.
The service accepts an immutable registry digest or local `sha256:` image ID, and requires a matching public source commit and OCI revision/source
labels. Author execution uses `/opt/nova/.venv/bin/python` from the locked environment.

```sh
docker buildx build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt \
  --build-context ldraw=/absolute/path/to/ldraw \
  --build-arg NOVA_SOURCE_REVISION="$PUBLIC_COMMIT" \
  -f second-brick/engine/Dockerfile \
  -t "registry.example/ldraw-nova:$PUBLIC_COMMIT" .
```

Run the service from a clean export of the same public fork commit (Python 3.12+).
Only the service environment contains its authentication secret; the subprocess
Docker client receives a fresh minimal environment and empty credential config.
The job image receives only the explicit non-secret variables shown in server.py.

```sh
export NOVA_ENGINE_IMAGE='registry.example/ldraw-nova@sha256:...'
export NOVA_ENGINE_VERSION="$PUBLIC_COMMIT"
export NOVA_ENGINE_SOURCE_URL="https://github.com/noahgsolomon/ldraw-nova/tree/$PUBLIC_COMMIT"
export NOVA_ENGINE_WORKSPACES=/var/lib/nova-engine
export NOVA_ENGINE_TOKEN='set-a-random-secret-at-least-32-characters-long'
python3 second-brick/engine/server.py
```

Optional configuration: `NOVA_ENGINE_BIND` (default 127.0.0.1),
`NOVA_ENGINE_PORT` (8788), `NOVA_ENGINE_TIMEOUT` (120 seconds total per command, maximum 600),
`NOVA_ENGINE_CREATE_TIMEOUT` (30 seconds for inert container creation; 1–120 and no
greater than the total command timeout),
`NOVA_ENGINE_DOCKER` (absolute executable path), and
`NOVA_ENGINE_DOCKER_SOCKET` (absolute local socket path). There is no remote Docker
URL, image name or command executable accepted from API requests.

Slow development hosts using Docker's VFS storage driver may need
`NOVA_ENGINE_CREATE_TIMEOUT=90`. Creation still counts toward the total command
deadline. Volume creation has its own 10-second bound. Shutdown waits at most
the container creation timeout plus 35 seconds for pending creation and cleanup;
unconfirmed cleanup keeps execution unavailable.

If a pending reservation cannot be resolved automatically, keep the service
stopped. Restart the dedicated Docker daemon so old creation requests cannot
still complete, inspect and remove the exact container and paired volume named
by each affected file in `NOVA_ENGINE_WORKSPACES/reservations`, then remove only
those recovered reservation files and restart the service. Do not clear a
pending marker solely because a Docker list command is empty. A crash between
successful removal and its journal update can require this conservative manual
recovery too. Preserve the journal when replacing the service process or image.

## API

All POST bodies use `Content-Type: application/json` and `Content-Length`.
Workspace requests require `Authorization: Bearer <NOVA_ENGINE_TOKEN>`.

- `GET /health`, `GET /source`: public `{ok,engineVersion,sourceUrl,license,image}`.
- `GET /v1/ready`: the same metadata, after validating bearer authentication;
  consumers use this before generation to verify both credentials and release.
- `POST /v1/workspaces`: `{files:[{path,text}]}` (files optional); returns
  `{id,engineVersion,sourceUrl,license,image}`.
- `DELETE /v1/workspaces/:id`: deletes source, generated files and call cache.
- `POST /v1/workspaces/:id/calls`: returns `{text,images?:string[]}`. Images are
  PNG/JPEG data URLs, at most four, each at most 2 MiB. A complete UTF-8 `read`
  result may contain up to 5 MiB; writes and other result text remain limited to
  1 MiB. Command stdout/stderr is bounded to 64 KiB. Encoded JSON responses have
  a separate 16 MiB limit, except reads allow worst-case sixfold text escaping
  plus the existing image allowance and 4 KiB overhead (about 40.67 MiB).
  The independent 64 MiB response cache reserves actual encoded space for
  non-mutating reads and the full allowed response before commands or writes.
  A large model read can therefore be followed by its small instruction file;
  oversized reads are refused, never truncated.
  An error returns `{error}` with an appropriate HTTP status.

Every call includes a unique `idempotencyKey` (1–96 ASCII letters, digits,
underscores, hyphens). Reuse the same key and exactly the same JSON body for
transport retries. Supported call bodies:

```json
{"idempotencyKey":"guide","action":"read","source":"toolkit","path":"instructions.md"}
{"idempotencyKey":"files","action":"list","source":"toolkit","path":"docs/agent"}
{"idempotencyKey":"author","action":"write","path":"generate.py","text":"from pathlib import Path\nprint('author here')\n"}
{"idempotencyKey":"build","action":"run","kind":"python","path":"generate.py","args":[]}
{"idempotencyKey":"search","action":"run","kind":"cli","args":["discover","search","submodels","rounded shell","--engine","fts","--limit","3"]}
{"idempotencyKey":"board","action":"run","kind":"cli","args":["part-board","3001.dat","--outdir","board"],"imagePaths":["board/part-3001.png"]}
{"idempotencyKey":"export","action":"read","path":"model.mpd"}
```

Use actual generated image filenames from `list` or command output; the board
filename above is illustrative. `source` defaults to `workspace`; `toolkit` is
read/list only. `list` accepts an omitted path for its source root. All workspace
paths are relative to `/job`; the full public toolkit is at `/opt/nova`. Python
runs a `.py` file already in the workspace. `args` is always an argument array,
never a shell command. `imagePaths` can accompany any call; alternatively `read`
a workspace PNG/JPEG directly. Unsupported file types are not exposed as images.

The consumer remains responsible for exact catalog/color admission, whole-model
geometry and connection validation, revision preservation, truthful physical-test
status, delivery and any separately approved model-provider budget. Runtime
success never bypasses those application gates.

## Verification

```sh
python3 -m unittest discover -s second-brick/engine -p 'test_*.py' -v
```

Tests cover real HTTP auth/API behavior, idempotency across service reconstruction,
serialization, bounds, archive attacks, sandbox command assembly and cancellation.
An actual published-image smoke should additionally run `doctor`, FTS discovery,
a small Python generator, a real PNG part board, and instruction rendering, then
verify denied network/root writes and timeout cleanup before release.
