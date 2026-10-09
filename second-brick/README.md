# 2nd Brick's Nova fork workspace

This public AGPL fork retains the complete upstream construction toolkit,
documentation, examples, reference database and source notices. Its imported base
is `1766e998b05b0b1c398b37ec786da2295f1d1fcd` in
<https://github.com/noahgsolomon/ldraw-nova>. New upstream changes require
validation before updating the deployed source pin.

The requested direction is to use Nova's actual authoring process: discover and
inspect construction references and exact parts, write reusable plans and Python
generators, compose submodels with anchors and snapping, inspect actual renders
throughout construction, and preserve reviewed source and instructions.

Read [the integration contract](INTEGRATION.md) for how this connects to the
existing exact-catalog importer and compiler. **Passing digital checks is not evidence of improved design quality or physical
buildability.**

## Run the full toolkit

Install the upstream locked environment as described in
[tooling](../docs/agent/tooling.md), and configure these non-secret paths:

```sh
export NOVA_PYTHON=/absolute/path/to/nova/.venv/bin/python
export NOVA_LDRAW_DIR=/absolute/path/to/ldraw
# Optional, when LeoCAD needs a platform-specific headless wrapper:
export NOVA_RENDER_BIN=/absolute/path/to/renderer/bin

./second-brick/run cli doctor
./second-brick/run cli discover search submodels 'arched window' --engine fts --limit 3
./second-brick/run cli part-board 3001.dat 3020.dat --colour 15 --outdir output/shortlist
./second-brick/run python /absolute/path/to/project/generate.py
./second-brick/run test -q
```

The launcher uses this fork's source even when borrowing an existing compatible
Python environment. It removes inherited credentials and uses a dedicated local
home/cache. It is **not a filesystem or network sandbox**. Run trusted local
authoring only; hosted arbitrary-code execution requires a real isolated worker.
Upstream Jev clients receive no credentials through this launcher. Offline FTS
is explicit; approved catalog reranking remains in the external 2nd Brick adapter.

For normal Codex authoring, read [upstream instructions](../instructions.md),
then the relevant family, discovery and modular-construction guides. This
local launcher does not start a nested Codex process or buy model calls. For
hosted authoring, [the engine service](engine/README.md) exposes bounded workspace
and image operations and executes each command in a separate Docker sandbox.
The host agent remains responsible for design decisions and visual review.

## Ownership and publication

Upstream code remains under [AGPL-3.0](../LICENSE); keep its notices and authorship.
The launcher and these fork notes are also provided under AGPL-3.0-only. Reference
assets and LDraw parts retain their own licenses. Generated models containing
copied references need those references' attribution and license review.

The owner approved publishing this engine under AGPL on October 8, 2026. The
public repository is <https://github.com/noahgsolomon/ldraw-nova>; its imported
history and upstream attribution are retained. Do not upload application secrets,
private catalog snapshots, customer models, or unrelated application source.

The engine exposes its exact deployed commit and public source URL at `/source`.
Its host must also make that source offer available to users. The surrounding
application adapter communicates through documented HTTP and file contracts; a
process boundary alone does not determine the scope of license obligations.
Documentation/reference assets retain [their separate notices](../ATTRIBUTION.md).

See [verification](VERIFICATION.md) for checks actually completed and
[the engine deployment guide](engine/README.md) for pinned-image configuration.
Publishing this source does not deploy the controller or connect a personal Codex
subscription to production jobs.
