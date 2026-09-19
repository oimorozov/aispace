import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


def ready(url, process):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Тестовый сервер завершился до запуска")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, ConnectionError):
            time.sleep(0.1)
    raise RuntimeError("Тестовый сервер не ответил")


def main():
    processes = []
    backend_port, github_port = free_port(), free_port()
    with tempfile.TemporaryDirectory(prefix="aispace-github-e2e-") as directory:
        base_url = f"http://127.0.0.1:{backend_port}"
        github_url = f"http://127.0.0.1:{github_port}"
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT / "backend" / "src"),
            "AISPACE_DATA_DIR": str(Path(directory) / "data"),
            "AISPACE_FRONTEND_DIR": str(Path(directory) / "dist"),
            "AISPACE_ALLOWED_ORIGINS": base_url,
            "AISPACE_GITHUB_MOCK_PORT": str(github_port),
            "AISPACE_GITHUB_MOCK_URL": github_url,
            "AISPACE_E2E_BASE_URL": base_url,
            "AISPACE_E2E_BACKEND_URL": base_url,
        }
        try:
            subprocess.run(
                [
                    "npm",
                    "--prefix",
                    "frontend",
                    "run",
                    "build",
                    "--",
                    "--outDir",
                    environment["AISPACE_FRONTEND_DIR"],
                ],
                cwd=ROOT,
                env=environment,
                check=True,
            )
            for command, url in [
                ([sys.executable, "e2e/mock_github.py"], f"{github_url}/audit"),
                (
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "e2e.github_app:app",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(backend_port),
                        "--no-access-log",
                    ],
                    f"{base_url}/api/health",
                ),
            ]:
                process = subprocess.Popen(
                    command, cwd=ROOT, env=environment, start_new_session=True
                )
                processes.append(process)
                ready(url, process)
            return subprocess.run(
                [
                    "npx",
                    "playwright",
                    "test",
                    "--config",
                    "e2e/playwright.config.ts",
                    "github.spec.ts",
                    *sys.argv[1:],
                ],
                cwd=ROOT,
                env=environment,
                check=False,
            ).returncode
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


if __name__ == "__main__":
    sys.exit(main())
