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

The service's 90 standard-library tests pass, covering authentication, exact
source/image pins, workspace quotas, path and tar attacks, idempotency after
restart, concurrency, sandbox flags, timeouts, shutdown admission/drain races and uncertain Docker creation cleanup. Python compilation
and whitespace checks pass. These are service tests; the actual image build and
HTTP-to-Docker smoke must be recorded separately before rollout. No paid model
calls or production deployment were part of these checks.
