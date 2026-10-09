#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build an immutable, reproducible offline discovery seed from public sources.

Run only when building the runtime image. Jobs receive an independent writable
copy; no workspace or application input is ever added to this image seed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat


def normalize_mtime(path: Path):
    """Make an image-owned index input stable across whole-second OCI archives."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError('Cache seed timestamps require ordinary files')
    seconds_ns = metadata.st_mtime_ns // 1_000_000_000 * 1_000_000_000
    if metadata.st_mtime_ns != seconds_ns:
        os.utime(path, ns=(metadata.st_atime_ns, seconds_ns), follow_symlinks=False)


def normalize_part_index_inputs(library: Path):
    # Match get_parts' signature inputs exactly. This is the image's copied
    # official library, not a runtime change to native cache invalidation.
    for part in sorted((library / 'parts').glob('*.dat')):
        normalize_mtime(part)


def main():
    from ldraw_tools.common import CACHE, get_parts, library_path
    from ldraw_tools.discovery import DiscoveryIndex

    destination = Path('/opt/nova-cache-seed')
    if destination.exists():
        raise RuntimeError('Refuse to replace an existing image cache seed')
    # Docker save/load can discard DAT nanoseconds. Normalize before get_parts
    # records its library signature, or it will later rewrite parts.lst and
    # invalidate the much larger discovery index in the bounded runtime cache.
    normalize_part_index_inputs(library_path())
    parts = get_parts()
    # OCI layers may round metadata to whole seconds. The discovery fingerprint
    # includes parts.lst mtime, so normalize it before computing that fingerprint.
    normalize_mtime(parts.path)
    index = DiscoveryIndex(parts)
    report = index.ensure()
    if not report['counts'].get('parts') or not report['counts'].get('models') or not report['counts'].get('submodels'):
        raise RuntimeError('The offline discovery seed is incomplete')
    files = [p for p in CACHE.rglob('*') if p.is_file() and not p.is_symlink()]
    total = sum(p.stat().st_size for p in files)
    if total > 112 * 1024 * 1024:
        raise RuntimeError('Offline cache seed exceeds its 112 MiB budget; review cache limits explicitly')
    for p in CACHE.rglob('*'):
        if p.is_symlink() and not p.resolve().is_relative_to('/opt/ldraw'):
            raise RuntimeError('Seed contains a link outside the immutable public LDraw library')
    shutil.move(CACHE, destination)
    CACHE.mkdir(mode=0o755)
    print(json.dumps({'cacheBytes': total, 'counts': report['counts'], 'signature': report['signature']}))


if __name__ == '__main__':
    main()
