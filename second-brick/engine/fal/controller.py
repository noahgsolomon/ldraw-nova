# SPDX-License-Identifier: AGPL-3.0-only
"""Always-on CPU supervisor for the existing production Studio worker."""
from pathlib import Path

import fal
from fal.container import ContainerImage


class StudioController(fal.App):
    machine_type = "L"
    min_concurrency = 1
    max_concurrency = 1
    max_multiplexing = 10
    keep_alive = 60
    startup_timeout = 600
    termination_grace_period_seconds = 3600
    secrets = ["SECOND_BRICK_WORKER_CONFIG"]
    image = ContainerImage.from_dockerfile(
        str(Path(__file__).with_name("Controller.Dockerfile")), context_dir=Path(__file__).parent, dockerignore=[])

    def setup(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        import threading
        import time

        sys.path.insert(0, "/opt/fal-worker")
        from sandbox_engine import NamespaceRunner
        from sandbox import toolkit_server

        config = json.loads(os.environ["SECOND_BRICK_WORKER_CONFIG"])
        self.stopping = threading.Event()
        self.activation = threading.Event()
        self.worker = None
        self.ready = False
        self.starts = 0
        server = toolkit_server()
        state = Path("/data/second-brick/studio")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Check the durability operations used by the paid-submission receipts.
        probe = state / "storage-probe"
        with probe.open("wb") as handle:
            handle.write(b"durable")
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        probe.unlink()
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "NODE_ENV": "production", **config["worker"]}
        env["NOVA_ENGINE_URL"] = "http://127.0.0.1:8788"
        engine_config = server.Config(
            root=state / "nova", toolkit=Path("/opt/nova"), image=config["runtime_image"],
            version=env["NOVA_ENGINE_VERSION"], token=env["NOVA_ENGINE_TOKEN"],
            source_url="https://github.com/noahgsolomon/ldraw-nova/tree/" + env["NOVA_ENGINE_VERSION"],
            max_workspaces=32,
        )
        self.runner = NamespaceRunner(engine_config)
        self.runner.preflight()
        self.engine = server.Engine(engine_config, runner=self.runner)
        self.server = server.Server(("127.0.0.1", 8788), self.engine)
        self.engine_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.engine_thread.start()
        # The image and all configured hosted-agent/schema checks are unchanged.
        preflight = subprocess.run(["/usr/local/bin/node", "/app/preflight.cjs"], cwd="/app", env=env, timeout=240)
        if preflight.returncode:
            self.server.shutdown()
            raise RuntimeError("Studio provider/database preflight failed")
        cache = state / "design-runs"
        cache.mkdir(exist_ok=True, mode=0o700)
        self.local = tempfile.TemporaryDirectory(prefix="second-brick-controller-")
        runtime = Path(self.local.name)
        (runtime / "artifacts").mkdir()
        (runtime / "artifacts/design-runs").symlink_to(cache, target_is_directory=True)
        (runtime / "lib").symlink_to("/app/lib", target_is_directory=True)
        self.enabled_file = state / "controller-enabled"
        if config.get("enabled", False) or self.enabled_file.exists():
            self.activation.set()

        def supervise():
            delay = 2
            while not self.stopping.is_set():
                if not self.activation.wait(0.5):
                    continue
                self.ready = False
                self.starts += 1
                process = subprocess.Popen(["/usr/local/bin/node", "/app/worker.cjs"], cwd=runtime,
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                self.worker = process
                for line in process.stdout:
                    print(line.rstrip(), flush=True)
                    if "design worker online" in line:
                        self.ready = True
                        delay = 2
                code = process.wait()
                self.ready = False
                # Only this reaped child's local lock is removed; paid receipts
                # remain on /data. No second worker can use this private cwd.
                (runtime / "artifacts/design-worker.lock").unlink(missing_ok=True)
                if not self.stopping.is_set():
                    print(json.dumps({"event": "worker_supervisor_restart", "exit_code": code}), flush=True)
                    self.stopping.wait(delay)
                    delay = min(delay * 2, 30)
        self.supervisor = threading.Thread(target=supervise, daemon=True)
        self.supervisor.start()
        deadline = time.monotonic() + 180
        while self.activation.is_set() and not self.ready:
            if time.monotonic() > deadline:
                self.handle_exit()
                raise RuntimeError("Studio worker did not become ready")
            time.sleep(0.2)

    @fal.endpoint("/status")
    def status(self) -> dict:
        return {"online": self.ready and self.worker is not None and self.worker.poll() is None,
                "worker_starts": self.starts, "engine_online": self.engine_thread.is_alive(),
                "machine_type": "L", "min_concurrency": 1, "max_concurrency": 1, "max_multiplexing": 10}

    @fal.endpoint("/verify")
    def verify(self) -> dict:
        import json
        import urllib.request
        import uuid
        from verify_sandbox import run_checks
        report = run_checks()
        def request(path, method="GET", body=None):
            req = urllib.request.Request("http://127.0.0.1:8788" + path, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + self.engine.config.token, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=150) as response:
                return json.load(response)
        request("/v1/ready")
        workspace = request("/v1/workspaces", "POST", {"files": [{"path": "verify.py",
            "text": "from pathlib import Path\nPath('result.txt').write_text('protocol verified')\nprint('verified')\n"}]})
        route = "/v1/workspaces/" + workspace["id"]
        try:
            result = request(route + "/calls", "POST", {"idempotencyKey": uuid.uuid4().hex,
                "action": "run", "kind": "python", "path": "verify.py"})
            if "Exit code: 0" not in result["text"]:
                raise RuntimeError("Nova HTTP execution verification failed")
            saved = request(route + "/calls", "POST", {"idempotencyKey": uuid.uuid4().hex,
                "action": "read", "path": "result.txt"})
            if saved["text"] != "protocol verified":
                raise RuntimeError("Nova HTTP workspace verification failed")
        finally:
            request(route, "DELETE")
        return {**report, "authenticated_workspace_protocol": "passed"}

    @fal.endpoint("/activate")
    def activate(self) -> dict:
        import os
        import time
        with self.enabled_file.open("wb") as handle:
            handle.write(b"enabled")
            handle.flush()
            os.fsync(handle.fileno())
        self.activation.set()
        deadline = time.monotonic() + 180
        while not self.ready and time.monotonic() < deadline:
            time.sleep(0.2)
        if not self.ready:
            raise RuntimeError("Worker did not become ready after activation")
        return self.status()

    def handle_exit(self):
        import signal
        self.stopping.set()
        if self.worker is not None and self.worker.poll() is None:
            self.worker.send_signal(signal.SIGUSR1)

    def teardown(self):
        import subprocess
        self.handle_exit()
        if self.worker is not None:
            try:
                self.worker.wait(timeout=3500)
            except subprocess.TimeoutExpired:
                self.worker.terminate()
                self.worker.wait(timeout=30)
        self.runner.close()
        self.server.shutdown()
        self.server.server_close()
