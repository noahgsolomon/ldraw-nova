# Integrating the full Nova authoring workflow with 2nd Brick

## Direction and current status

The requested direction is to adopt Nova's actual authoring workflow: discover
construction references, inspect real parts, write reproducible plans and
generators, assemble modules, render unfinished work, and revise it before final
delivery. This fork keeps that toolkit available instead of reducing it to a
planning prompt.

The public fork is <https://github.com/noahgsolomon/ldraw-nova>, based on its
imported commit `1766e998b05b0b1c398b37ec786da2295f1d1fcd`. The owner approved
publishing the modified engine under AGPL on October 8, 2026. The separate
application adapter is not copied into this repository.

[The engine service](engine/README.md) implements authenticated, version-pinned
workspaces, CLI/Python execution, bounded image transport, idempotency and
sandbox cleanup. The application integration adds trusted exact-part bindings,
source-bound instruction admission and compiler enforcement behind an explicit
rollout flag. Local checks and source publication do not deploy the production
controller; fresh agent configuration and a separately hosted engine are needed.
See [verification](VERIFICATION.md) for evidence and outstanding checks.

## Use the upstream tools as tools

Start with [agent instructions](../instructions.md), then the relevant guides:

| Authoring need | Existing toolkit |
| --- | --- |
| Find complete models, submodels and parts | [Reference discovery](../docs/agent/reference-discovery.md) |
| Compare actual shapes and surfaces | [Visual design and part boards](../docs/agent/visual-design.md) |
| Compose plans, generators, modules and named anchors | [Complex models](../docs/agent/complex-models.md) |
| Inspect real connectors and candidate attachments | [Snapping](../docs/agent/snapping.md) |
| Study and retain authored construction steps | [Build manuals](../docs/agent/build-manuals.md) |
| Assess the native checks and their limits | [Validation](../docs/agent/validation.md) |
| Work with specific construction families | [Vehicle](../docs/agent/vehicles.md), [Technic](../docs/agent/technic.md), [mechanism](../docs/agent/mechanisms.md), and [spaceship](../docs/agent/spaceships.md) guides |

Nova is not an independently running designer or a model provider. A host agent
reads the brief and references, chooses tool calls, writes source, inspects the
returned images, and decides what to change. The Python toolkit builds, measures,
renders, searches and validates what that agent supplies. In a local experiment,
the normal Codex task is that host. Using this fork does not automatically connect
a Codex subscription to a production controller.

The launcher accepts `cli`, `python`, and `test` modes. Configure an absolute
`NOVA_PYTHON` pointing to a Python environment with the locked upstream
dependencies, an absolute `NOVA_LDRAW_DIR` pointing to the installed LDraw library,
and, when needed, `NOVA_RENDER_BIN` for the renderer tool directory. Renderer and
display dependencies must also be installed. Consult the launcher for its exact
environment handling. These variables contain filesystem paths, not credentials.

Example commands, from the fork root after setup:

```sh
./second-brick/run cli doctor
./second-brick/run cli --help
./second-brick/run cli discover search submodels 'rounded shell' --engine fts --limit 5
./second-brick/run cli part-board 3001 3020 --outdir output/part-shortlist
./second-brick/run cli build output/model.plan.json --output output/model.mpd
./second-brick/run cli render output/model.mpd --outdir output/model-views --views home front right top
./second-brick/run python output/generate.py
./second-brick/run test tests/test_discovery.py
```

The part-board IDs above are toolkit examples, not a claim that those exact
part/color combinations have passed 2nd Brick's catalog filter. Use the chosen
exact bindings for real design work. Use fresh output paths for attempts rather
than replacing evidence. Discovery requires the upstream reference database and
source data; a missing resource is a setup problem, not an empty search result.

Use explicit `--engine fts` for offline discovery. Upstream instructions describing
a live Jev access check do not authorize a paid call. Jev is optional; the current
2nd Brick adapter has a separately bounded reranker for eligible part candidates.
That budget does not automatically govern upstream searches of whole models or
submodels. Do not pass database or provider credentials to generator scripts.

## Exact catalog first, final acceptance last

The existing 2nd Brick application has an original file-based bridge. Run its
commands in that application's checkout, with its documented Node version and
dependencies. The fork itself does not provide these npm commands.

```sh
# Local searches of an existing eligible snapshot; no database call.
npm run design:nova -- search artifacts/catalog.json "curved slope" --color White --limit 12

# ids.json contains the chosen exact element IDs, not generic design IDs.
npm run design:nova -- bindings artifacts/catalog.json artifacts/ids.json artifacts/bindings.json

# Import into a new attempt directory. These paths are in the application checkout.
npm run design:nova -- import artifacts/catalog.json artifacts/model.mpd artifacts/bindings.json artifacts/import-001 --title "Model title" --summary "Model description" --frames

# Re-audit an imported source after any edits.
npm run design:nova -- check-source artifacts/catalog.json artifacts/source.json --frames --joints
```

For exact mapping derivation, the application exporter needs `LDRAW_DIR` and
`LDRAW_LATEST` configured to the absolute official library path. Its flags and the
snapshot must agree. Do not assume the fork launcher's environment carries into a
separate application process.

A public snapshot can be captured with the separately authorized application
command `npm run catalog:nova -- artifacts/catalog.json --frames --joints`.
That command requires database access. Prefer a supplied valid snapshot for an
offline run. Snapshot timestamps, observation freshness, eligibility rules,
geometry and hashes remain authoritative; a fresh file timestamp cannot renew old
availability observations. A database observation is not a stock reservation.

Search and reference study may suggest many constructions. Before accepting any
emitted physical piece, resolve its exact file, color, element identity and
geometry through the bridge. Never substitute an approximate alias, nearest
color, omitted component or rounded transform to force acceptance. Reused
references can contain unsupported parts even when their native renders look
correct. Preserve that evidence and adapt deliberately or reject the candidate.

The existing importer retains nested MPD source paths and ordered steps, expands
inherited colors, checks physical-leaf counts, and verifies compiled pose
correspondence. It supports upright placements and restricted, exact side-stud
frame inference. It is not a universal LDraw importer. Arbitrary rotations,
reflections, scale, unresolved geometry, unsupported directives, off-grid poses,
inferred joints, nested frames and rigid composite wheel packages can be refused.
Native Nova support for a construction does not imply application support.

An imported draft still needs authored instruction text, the ordinary workshop
check, actual view inspection, a current recorded review, and finalization. The
existing workshop can then export the build pack. An import audit or native render
alone does not create a customer-ready design. Consult the application's
`docs/nova-catalog-evaluation.md` and `docs/codex-design-workshop.md` for the complete
current command workflow.

## Production admission contract to implement

The host should submit a source-bound proposal, not declare a model ready. The
following is the required integration protocol; it is **not an implemented API**.

| Proposal evidence or trusted host input | Admission requirement |
| --- | --- |
| Brief, requested size and capability flags | Bind the exact request; enforce the enabled construction capabilities and physical-piece range. |
| Plans, generator source, generated MPD and hashes | Retain reproducible inputs and every attempted result; reject stale correspondence rather than silently rebuilding a different proposal. |
| Exact catalog snapshot and part/color bindings | Recheck policy/freshness, expand every physical leaf, and reconcile exact quantities with the compiled BOM. |
| Authored instruction sidecar | Bind useful titles/descriptions to source module occurrences and step paths, then to the flattened compiler steps; reject missing, ambiguous or stale mappings. |
| Server-resolved revision baseline and selection | Preserve world coordinates and exact protected identities, poses and attachments. Pass the frozen scope into compilation and retain the existing protection checks. |
| Server-resolved Rebuild inventory | Enforce exact owned element/color quantities with the existing inventory checks; catalog eligibility alone is insufficient. |
| Connection and joint contracts | Preserve supported explicit contracts; reject constructions whose essential mechanics cannot be represented, without flattening or inventing joints. |
| Reference/source attribution and license records | Retain copied-source headers, extraction manifests and changes; distinguish studied techniques from copied assets. |
| Native diagnostics, actual renders and review evidence | Keep these as evidence; still run the complete application compiler, full-model renders and the existing acceptance/review policy. |

Two existing bridge behaviors require changes before scoped production revisions:
its compiler call has no revision scope, so it normally normalizes horizontal
origin, and its instruction text is a placeholder. The production path must compile
in the frozen baseline coordinate system and supply real authored instructions.
Source occurrence identifiers must remain traceable, but generated brick numbering
alone is not preservation evidence; the existing application checks physical
identity and attachment as well. A generator must not be able to replace the
trusted baseline or inventory with its own claims.

The current import compiler path also has joints disabled. A future extension
needs exact joint metadata and validation, not an inference that two nearby parts
form a working hinge. Until that exists, retain the native model and diagnostics
as an unsupported result. Do not weaken the compiler to make a larger fraction of
Nova outputs pass.

## Host execution and operational boundaries

The engine service provides persistent logical workspaces and bounded tool/image
transport. Each execution stages the workspace into an isolated, non-root,
network-disabled container with resource limits; workspace exports are validated
before persistence. The service token and Docker socket stay outside generated
code. Read its deployment guide for the host trust boundary and limits.

The launcher is an environment adapter, not an execution sandbox. Before production
use, run generated code in an isolated workspace with resource and execution
limits, cancellation, controlled file access and an explicit network policy. Keep
application, database, customer and provider secrets outside generated code.
Enforce call/spend limits across retries and resumed sessions rather than resetting
them with each process. Publish accepted artifacts only through existing ownership
and delivery controls.

Cache geometry and renders against source, library, flags and view parameters.
Changed source must invalidate reviews and relevant cached artifacts. The host
must actually inspect the images; recording a list of view names does not establish
that inspection occurred. Diagnostics from native, compiler and visual checks must
remain distinguishable.

## Rollout and evidence

1. Make the local pinned runtime reproducible. Exercise discovery, a real part
   board, a generated multi-module model, rendering and import without production
   jobs or paid model calls. Record dependency and source revisions.
2. Implement and test the admission contract, including refusal cases: wrong exact
   color or quantity, unsupported mechanics, protected-coordinate changes, missing
   instruction text, stale reference hashes and mismatched physical BOMs.
3. Run an owner-reviewed comparison of fresh first results across representative
   animals, objects and structures, using equivalent catalog constraints, size
   targets, review requirements and effort limits. Preserve failures and distinguish
   reuse from fresh work. Include a scoped revision and a Rebuild case once supported.
4. Review the concrete production package, license/source offer and asset notices.
   Connect the host runtime behind an explicit rollout flag, refresh the corresponding
   agent/tool configuration, and retain a rollback route. Local success and a merged
   source change are not a controller deployment.
5. Expand only after the owner reviews first-result resemblance, unwanted revision
   changes, digital acceptance, instruction quality, failure recovery and measured
   end-to-end cost/latency. No quality or savings threshold has been established by
   merely forking the toolkit.

Neither native checks nor the application compiler prove physical strength,
mechanism reliability or real stock availability. Record actual physical-test
notes separately when a model is built. Generated image review, a digital pass and
a photographed physical build are different evidence.

## Licenses and provenance

Keep the upstream [AGPL-3.0 license](../LICENSE), copyright notices and relevant
asset-specific licenses with their sources. Record modifications to this fork.
Do not relabel copied upstream source or reference constructions as original
2nd Brick work. The instructions and examples are part of the material whose
provenance needs retention, not just the Python files.

AGPL section 4 describes verbatim copying; section 5 covers conveyed modified
versions and distinguishes genuinely separate aggregates; section 13 addresses
Corresponding Source for users interacting remotely with a modified version.
Section 2 addresses when output itself constitutes a covered work. These provisions
do not justify either a blanket assurance that a service boundary removes
obligations or a blanket assertion that every surrounding application must adopt
the same license. Determine the applicable scope for the concrete integration and
source offer before deployment. Copied model/reference assets require their own
provenance and license review regardless of how the generator is hosted.
