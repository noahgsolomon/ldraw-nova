# Local fork verification

The fork starts from the public imported commit
`1766e998b05b0b1c398b37ec786da2295f1d1fcd`. Changes include portable geometry
expansion, stricter Technic coverage checks, the local launcher and a separate
sandboxed engine API. The application integration is maintained in 2nd Brick's
repository, not copied into this fork.

Runtime: Python 3.12.14, pyldraw3 1.7.0, numpy 2.5.2, jsonschema 4.26.0,
LeoCAD 23.03 through Xvfb/software rendering, official LDraw library. The launcher
was verified to import this fork's source and exclude inherited application and
provider credential variables. This is environment isolation, not an OS sandbox.

Fresh smoke results:

- Offline reference discovery returned candidates.
- The upstream five-piece modular bridge built and passed native geometry and
  full-contact checks with no diagnostics.
- Three complete-model views rendered.
- LeoCAD and Python BOMs matched all five physical placements.
- A three-part visual board rendered from actual library geometry.
- A two-step submodel manual rendered home/front step views and an overview.
- No paid model or Jev calls, customer/database operations, or deployment occurred.

Actual image review opened all three part-board cards, the bridge home view,
both instruction-step home views and the manual overview. Each silhouette is
fully visible. The previous claim that the curved-part preview was cropped was
incorrect: its 1000 × 800 PNG has 182/170/164/95 pixels of left/top/right/bottom
background margin. The other two cards have at least 97 pixels of margin on
every edge; the bridge home view has at least 149 pixels. All eleven generated
PNGs have background on every edge, including both highlighted manual steps
(at least 56 pixels) and the manual overview (at least 59 pixels).

`output/fork-smoke/raster-framing.json` records each image's SHA-256, dimensions,
foreground bounding box and four margins. The check compares every RGB pixel
with the solid corner background, including antialiasing pixels; it does not
modify images. The actual images were also inspected visually. No renderer
change is justified by these results. This verifies framing of these samples,
not arbitrary geometry or mechanical validity.

The original upstream run had **207 passed, 17 failed** in 190.10 seconds. The
Technic failures came from platform-dependent matrix/point arithmetic in NumPy's
BLAS path, not changed source geometry. Comparing all 34 definitions showed
identical point counts and bounds, with coordinate differences no larger than
`8.881784197001252e-16` LDU. Eleven resulting hashes differed from the reviewed
registry. Evaluating source-reference points in a fixed scalar order reproduces
**all 34 original hashes exactly**. The local Parts adapter makes that arithmetic
portable without global monkeypatching, library edits, coordinate rounding or
changes to recorded fingerprints. Embedded parts use the same expansion path.
`output/fork-smoke/technic-portability.json` records the comparison.

Incomplete source geometry cannot receive reviewed ports. Unknown Technic
geometry now also makes `coverage.complete` and `checks_passed` false, even with
no rigid contract; exploratory warning details remain available. New regressions
cover unchanged source/global dependency behavior, embedded parts, the published
pin fingerprint, altered geometry with unchanged bounds, incomplete geometry and
unknown interface coverage. The original Technic/connectivity checks pass all
45 tests, including invalid pin seating, keyed roll, incomplete grip, occupied
interfaces and the authored structural recipes. This restores those digital
checks, not proof of physical fit or strength.

After the fork update and these fixes, the complete suite passed **232 tests**
with no failures or skips in 222.82 seconds. This includes the newer upstream
atlas tests and six new portability/coverage regressions. Reproduce with the
documented paths and `./second-brick/run test -q`.

Ignored local evidence is in `output/fork-smoke/`: native source, reports, real
images, test log and a SHA-256 artifact manifest. Generated artifacts are excluded
from the fork's source changes. This is a runnable local toolkit with explicit
validation gaps, **not a production-ready replacement, a new customer design, or
a physically tested build**. See [the integration contract](INTEGRATION.md) for
the rollout requirements.

## Engine service checks

The service's **124 standard-library tests** pass, covering authentication, exact
source/image pins, workspace quotas, path and tar attacks, idempotency after
restart, concurrency, sandbox flags, timeouts, shutdown admission/drain races,
normalized archive ownership, bounded workspace volumes, private offline cache
restoration, directory quotas, and durable recovery of uncertain Docker resource
creation across service restarts. They also cover complete 5 MiB model reads,
escaped response/cache budgets, subsequent instruction reads, and build-time
timestamp normalization. **One additional native regression** creates real parts
and discovery indexes, round-trips their inputs through a whole-second archive,
then verifies that native code reuses both indexes without rebuilding.

These are newly run checks. The **232-test full native suite** recorded above
predates this packaging/transport update and was not rerun; the native toolkit
implementation and dependencies are unchanged. Python compilation and whitespace
checks pass. No paid model calls or production deployment were part of these
checks.

### Current source and cold-loaded runtime proof

The tested runtime source is
`c0473bae3cc26722cf650bd5360443117203cbb6`, tree
`e4b2234a7136e0ffc6f44a6d6a9c9ba71b39f9c6`. Its immutable local image is
`sha256:248c5a5e6a945d007fe115a76d39c82215e02be8c5ba9e1da869222f2dd29c02`.
The service verifies matching OCI source/revision labels and authenticated
readiness. Later documentation-only commits do not change this image's source
identity.

To avoid duplicating large layers on the development host's VFS storage driver,
this image was built offline from the previously verified immutable image
`sha256:2d19c05ae8b78219d26a250fd3804c8e6ce3631ead33fc1645b071cd70d7edbd`.
A single filesystem layer applied the seven changed public source files, with
no deleted paths. It ran the canonical
`chmod -R a+rX,go-w /opt/nova /opt/ldraw` step, removed both old caches, executed
the committed `seed_cache.py`, and normalized the seed's public permissions.
The build genuinely regenerated the 94,732,158-byte seed; no stored signature
was manually substituted. The normal [engine Dockerfile](engine/Dockerfile)
remains the recipe for a full build from a clean source export and the official
LDraw library. An identical digest is not promised from future mutable OS
package repositories.

The saved archive was checked against all **9,584 allowed public source files**
in the pinned Git tree. All **24,856 top-level official DAT files** retained
their contents and modes. Their timestamps, and the generated parts index
timestamp, use whole seconds before native fingerprints are computed. Both seed
fingerprints recomputed correctly from the archived metadata. The tracked SQLite
shared-memory sidecar had only a timestamp change; its original bytes and mode
were verified. Checks also covered 49,390 runtime permission entries, including
source/library/seed directory traversal and the complete Python entrypoint
symlink chain for UID 65532.

The new image, its task-local base image and only the newly created build-cache
records were removed before loading the saved archive. The reloaded image had
the exact original image ID, config, size and nine filesystem layer IDs. The
646,058,564-byte compressed archive's SHA-256 is
`92be91af751b5241e72431c15a0bdd443b85442f59cada2e6042a11ff972a767`.
Unrelated images and pre-existing build-cache records were retained.

The actual app-to-HTTP-to-Docker smoke then passed in **38.094 seconds**:

- An unprivileged native command reused the restored FTS seed, searched it,
  built a plan, passed geometry/full-contact validation, and rendered a real PNG.
  The index inode, size, nanosecond timestamp and manifest stayed unchanged;
  discovery did not rebuild inside the bounded runtime cache.
- The application read a complete **5 MiB UTF-8 MPD**, then its small instruction
  sidecar. Strict admission preserved the exact source hash, poses, BOM and two
  authored steps. This transport fixture used two connected catalog bricks and
  comment padding with Unicode and escaped control characters to reach the
  limit; it is not a large-model performance benchmark.
- The **31,455,509-byte JSON response** replayed under the same idempotency key,
  and a subsequent small read still succeeded within the response-cache budget.
- A source one UTF-8 byte above 5 MiB was refused with HTTP 413. The returned
  native image was visually reviewed, and the workspace closed successfully.

The seed contained 35,257 part records, 1,821 models and 28,451 submodels. Local
evidence includes the exact incremental recipe and source manifest,
`image-source-seed-proof.json`, `roundtrip-removal-proof.json`,
`roundtrip-proof.json`, and the complete-read `smoke-report.json`, native PNG and
admission receipts. These generated artifacts are retained outside public source.

### Historical isolated containment and authoring proof

The clean source export at `9868c9626d82ddde6d5fc721f3d4978aaaeb70a4` built
immutable local image
`sha256:2d19c05ae8b78219d26a250fd3804c8e6ce3631ead33fc1645b071cd70d7edbd`.
The service verified its matching OCI source/revision labels. This is the prior
fresh-build proof and the inherited base for the current image; it did not verify
cache portability through save/load. The current proof above covers that case.

The real HTTP-to-Docker smoke passed all 13 recorded workspace calls, plus
authentication/readiness, workspace creation/deletion and idempotency assertions.
One author Python command completed eight native operations: `doctor`, PDF
`spec`, offline FTS discovery, plan `build`, full-contact geometry `validate`,
model `render`, `part-board`, and instruction `manual prepare`. Four real PNGs
returned through HTTP matched the visually reviewed model and part-board images
byte for byte. The complete native pipeline, including cold container creation
and cleanup, took 68.2 seconds on this development host.

The running author verified UID 65532, the 64 MiB workspace mount, denied network
and root writes, and absence of the Docker socket and service token. Its private
cache reused the prebuilt public index without rebuilding. A generated symlink
was refused at export with HTTP 400; the original workspace generation survived.
A separate sleeping author was observed writing its marker inside the running
container before the configured timeout returned HTTP 504. That unpublished
change was also discarded. Final checks found no matching containers, volumes,
or durable reservation files, and the service remained healthy.

This is local runtime evidence, not a production deployment, a paid provider
evaluation, or a physically tested construction. It proves these authoring and
containment paths; whole-model/catalog/revision admission remains the application's
responsibility. Local smoke reports, returned PNGs and native command evidence
were retained separately from the public source tree.
