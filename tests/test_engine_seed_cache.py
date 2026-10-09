# SPDX-License-Identifier: AGPL-3.0-only
import importlib.util
import io
import os
from pathlib import Path
import sqlite3
import tarfile
from unittest import mock

from ldraw_tools import common
from ldraw_tools.discovery import DiscoveryIndex


def test_seeded_native_indexes_are_reused_after_whole_second_image_archive(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('engine_seed_cache', Path(__file__).resolve().parents[1] / 'second-brick/engine/seed_cache.py')
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    image = tmp_path / 'image'
    library = image / 'library'
    (library / 'parts').mkdir(parents=True)
    (library / 'p').mkdir()
    (library / 'LDConfig.ldr').write_text('0 !COLOUR White CODE 15 VALUE #FFFFFF EDGE #333333\n')
    dat = library / 'parts/3001.dat'
    dat.write_text('0 Brick\n0 !LDRAW_ORG Part\n4 16 -40 0 -20 40 0 -20 40 0 20 -40 0 20\n')
    os.utime(dat, ns=(1_600_000_000_000_000_000, 1_700_000_000_999_999_999))
    models = image / 'models'
    models.mkdir()
    (models / 'a.mpd').write_text('0 FILE main.ldr\n0 Test model\n1 15 0 0 0 1 0 0 0 1 0 0 0 1 3001.dat\n0 NOFILE\n')
    database = image / 'source.sqlite'
    with sqlite3.connect(database) as connection:
        for name, value in [('PARTS', '3001.dat|Brick'), ('MODELS', 'a.mpd|Test model'), ('SUBMODELS', 'a.mpd|main.ldr|Test model')]:
            connection.execute(f'CREATE TABLE {name}_DESCRIPTIONS_JEV(full_description TEXT)')
            connection.execute(f'INSERT INTO {name}_DESCRIPTIONS_JEV VALUES(?)', (value,))
    for source in [models / 'a.mpd', database]:
        os.utime(source, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    monkeypatch.setattr(common, 'CACHE', image / 'cache')
    seed.normalize_part_index_inputs(library)
    parts = common.get_parts(library, shadows=[])
    seed.normalize_mtime(parts.path)
    index = DiscoveryIndex(parts, models, database=database, cache=image / 'discovery')
    report = index.ensure()
    assert report['counts'] == {'parts': 1, 'models': 1, 'submodels': 1}
    assert report['errors'] == []

    # Round-trip regular image files through tar with integer-second metadata.
    # Apply members at the same paths, as image save/load does; no extractall or
    # link traversal is needed for this fixture's read-through library symlinks.
    files = [path for path in image.rglob('*') if path.is_file() and not path.is_symlink()]
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w', format=tarfile.USTAR_FORMAT) as output:
        for path in files:
            metadata = path.stat()
            member = tarfile.TarInfo(path.relative_to(image).as_posix())
            member.size = metadata.st_size
            member.mtime = metadata.st_mtime_ns // 1_000_000_000
            with path.open('rb') as source:
                output.addfile(member, source)
    archive.seek(0)
    with tarfile.open(fileobj=archive) as restored:
        for member in restored:
            target = image / member.name
            assert target in files
            target.write_bytes(restored.extractfile(member).read())
            os.utime(target, ns=(member.mtime * 1_000_000_000, member.mtime * 1_000_000_000))
    before = (parts.path.read_bytes(), parts.path.stat().st_mtime_ns, index.path.read_bytes())
    with mock.patch.object(common, 'atomic_write', side_effect=AssertionError('parts index unexpectedly rebuilt')):
        reused_parts = common.get_parts(library, shadows=[])
    reused = DiscoveryIndex(reused_parts, models, database=database, cache=image / 'discovery')
    with mock.patch('ldraw_tools.discovery.sqlite3.connect', side_effect=AssertionError('discovery index unexpectedly rebuilt')):
        assert reused.ensure() == report
    assert (parts.path.read_bytes(), parts.path.stat().st_mtime_ns, index.path.read_bytes()) == before
