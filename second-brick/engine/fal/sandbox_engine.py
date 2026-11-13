# SPDX-License-Identifier: AGPL-3.0-only
"""Nova's unchanged transactional workspace API, with bounded local namespaces."""
from __future__ import annotations

import io
import tempfile
import threading
from pathlib import Path

from sandbox import execute, pack, toolkit_server, unpack


class NamespaceRunner:
    def __init__(self, config):
        self.config = config
        self.closing = False
        self.pending = set()
        self.guard = threading.Lock()
        self.slots = threading.BoundedSemaphore(config.parallel)

    def preflight(self):
        from verify_sandbox import isolation_check
        isolation_check()

    def healthy(self):
        return not self.closing

    def reap_orphans(self):
        # Each execution owns a disposable PID namespace, killed on parent exit.
        pass

    def close(self, *, timeout=None):
        self.closing = True
        with self.guard:
            for event in self.pending:
                event.set()
        return True

    def run(self, workspace: Path, body, cancelled):
        server = toolkit_server()
        while not self.slots.acquire(timeout=0.2):
            if cancelled.is_set() or self.closing:
                raise server.Refused("Engine command cancelled", 503)
        try:
            if cancelled.is_set() or self.closing:
                raise server.Refused("Engine command cancelled", 503)
            with self.guard:
                self.pending.add(cancelled)
            with server.workspace_archive(workspace) as archive:
                payload = {"archive": pack(archive.read()), "body": body}
            result = execute(payload, cancelled=cancelled)
            if cancelled.is_set() or self.closing:
                raise server.Refused("Engine command cancelled", 503)
            code, output = result["code"], result["output"]
            if not isinstance(code, int) or not isinstance(output, str) or len(output.encode()) > 65536:
                raise ValueError("Invalid sandbox result")
            staging = Path(tempfile.mkdtemp(prefix="generation-", dir=workspace.parent))
            try:
                server.import_archive(io.BytesIO(unpack(result["archive"])), staging)
            except BaseException:
                import shutil
                shutil.rmtree(staging)
                raise
            return code, output, staging
        finally:
            with self.guard:
                self.pending.discard(cancelled)
            self.slots.release()
