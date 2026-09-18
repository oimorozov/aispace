import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen

from dev import ROOT, available


def ready(url, process):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Server exited with status {process.returncode}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, ConnectionError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Server did not become ready: {url}")


def main():
    for port in (8000, 5173, 8766):
        if not available(port):
            print(
                f"Port {port} is in use. Stop the development servers before running E2E tests.",
                file=sys.stderr,
            )
            return 1
    processes = []

    def stop(signum=None, frame=None):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    with tempfile.TemporaryDirectory(prefix="aispace-e2e-") as data_dir:
        environment = {
            **os.environ,
            "AISPACE_DATA_DIR": data_dir,
            "AISPACE_WORKSPACE_ROOT": str(ROOT / "e2e" / "fixtures" / "projects"),
            "AISPACE_CODEX_HOME": str(ROOT / data_dir / "codex"),
            "AISPACE_CODEX_COMMAND_JSON": json.dumps(
                [sys.executable, str(ROOT / "e2e" / "fake_codex.py")]
            ),
            "AISPACE_FAKE_CODEX_AUDIT_URL": "http://127.0.0.1:8766/codex/audit",
        }
        commands = [
            (
                [sys.executable, "e2e/mock_provider.py"],
                "http://127.0.0.1:8766/v1/models",
            ),
            (
                [
                    "uv",
                    "run",
                    "--package",
                    "aispace-backend",
                    "uvicorn",
                    "aispace.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8000",
                ],
                "http://127.0.0.1:8000/api/workspace",
            ),
            (
                ["npm", "--prefix", "frontend", "run", "dev", "--", "--strictPort"],
                "http://127.0.0.1:5173",
            ),
        ]
        try:
            for command, url in commands:
                process = subprocess.Popen(
                    command, cwd=ROOT, env=environment, start_new_session=True
                )
                processes.append(process)
                ready(url, process)
            process = subprocess.Popen(
                [
                    "npx",
                    "playwright",
                    "test",
                    "--config",
                    "e2e/playwright.config.ts",
                    *sys.argv[1:],
                ],
                cwd=ROOT,
                env=environment,
                start_new_session=True,
            )
            processes.append(process)
            return process.wait()
        except KeyboardInterrupt:
            return 130
        except (OSError, RuntimeError) as error:
            print(str(error), file=sys.stderr)
            return 1
        finally:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            for process in reversed(processes):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
            for process in reversed(processes):
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == "__main__":
    sys.exit(main())
