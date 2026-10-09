"""Keep exact source fingerprints independent of BLAS point arithmetic."""
import shutil

import pytest
from ldraw import Matrix, Parts, Piece

from ldraw_tools.common import get_parts
from ldraw_tools.document import DocumentParts
from ldraw_tools.portable_geometry import SourcePointMatrix


def test_source_expansion_is_local_and_preserves_source(parts, tmp_path):
    root = tmp_path / 'source-library'
    shutil.copytree(parts.path.parent, root)
    source = ('0 Decimal transformed primitive\n'
              '1 16 1 2 3 .7071 0 .7071 0 1 0 -.7071 0 .7071 primitive.dat\n')
    (root / 'parts' / '3001.dat').write_text(source)
    (root / 'p' / 'primitive.dat').write_text('0 Primitive\n2 24 1 2 3 4 5 6\n')
    upstream_mul = Matrix.__mul__
    portable = get_parts(root, shadows=[])
    original = Parts(root / 'parts.lst')

    local_ref = next(obj for obj in portable.part(code='3001').objects if isinstance(obj, Piece))
    ordinary_ref = next(obj for obj in original.part(code='3001').objects if isinstance(obj, Piece))
    assert isinstance(local_ref.matrix, SourcePointMatrix)
    assert type(ordinary_ref.matrix) is Matrix
    assert local_ref.to_ldraw() == ordinary_ref.to_ldraw()
    assert Matrix.__mul__ is upstream_mul
    assert (root / 'parts' / '3001.dat').read_text() == source
    from ldraw_tools.common import jsonable
    points = [jsonable(point) for point in portable.geometry('3001').points]
    assert len(points) == 2
    assert points[0] == pytest.approx([3.8284, 4., 4.4142])
    assert points[1] == pytest.approx([8.071, 7., 4.4142])


def test_embedded_parts_and_library_parts_share_portable_expansion(parts):
    from ldraw import parse_model_result

    source = ('0 FILE main.ldr\n0 Model\n'
              '1 4 0 0 0 1 0 0 0 1 0 0 0 1 embedded.dat\n'
              '0 FILE embedded.dat\n0 Embedded part\n'
              '1 16 0 0 0 .7071 0 .7071 0 1 0 -.7071 0 .7071 3001.dat\n')
    model = parse_model_result(source).model
    overlay = DocumentParts(parts, model)
    local_ref = next(obj for obj in overlay.part(code='embedded').objects if isinstance(obj, Piece))
    assert isinstance(local_ref.matrix, SourcePointMatrix)
    assert overlay.geometry('embedded').complete


def test_existing_reviewed_pin_hash_is_reproduced_without_registry_edits(official):
    import hashlib
    import json
    from ldraw_tools.common import jsonable

    # Published upstream hash: this failed on Linux BLAS despite identical DATs.
    points = [jsonable(p) for p in official.geometry('2780').points]
    digest = hashlib.sha256(json.dumps(points, separators=(',', ':')).encode()).hexdigest()
    assert digest == '8bd50ea59162748510cb6ae2284c304983d52bc499589f9d8bc1e7192632b85d'
