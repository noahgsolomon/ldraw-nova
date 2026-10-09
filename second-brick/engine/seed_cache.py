#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build an immutable, reproducible offline discovery seed from public sources.

Run only when building the runtime image. Jobs receive an independent writable
copy; no workspace or application input is ever added to this image seed.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from ldraw_tools.common import CACHE, get_parts
from ldraw_tools.discovery import DiscoveryIndex


def main():
    destination = Path('/opt/nova-cache-seed')
    if destination.exists():
        raise RuntimeError('Refuse to replace an existing image cache seed')
    parts = get_parts()
    # OCI layers may round metadata to whole seconds. The discovery fingerprint
    # includes parts.lst mtime, so normalize it before computing that fingerprint.
    import os
    stamp = int(parts.path.stat().st_mtime)
    os.utime(parts.path, (stamp, stamp))
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
