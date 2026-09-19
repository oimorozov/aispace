import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

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


def api(url, data=None, method=None):
    request = Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def restart_fixture(base_url, github_url, provider_url):
    api(f"{github_url}/fixture", {"planner": "valid"})
    api(
        f"{base_url}/api/settings",
        {
            "execution_mode": "api",
            "api_key": "local-test-key",
            "model": "planner-fixture",
            "base_url": f"{provider_url}/v1",
        },
        "PATCH",
    )
    selection = api(
        f"{base_url}/api/github/selections",
        {
            "repository": "demo/plan",
            "repository_id": 400,
            "issues": [
                {"id": 400000 + number, "number": number} for number in range(1, 5)
            ],
        },
    )
    plan = api(f"{base_url}/api/github/plans", {"selection_id": selection["id"]})
    deadline = time.monotonic() + 10
    while plan["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        plan = api(f"{base_url}/api/github/plans/{plan['id']}")
    if plan["status"] != "completed":
        raise RuntimeError("Не удалось подготовить проверку перезапуска импорта")
    payload = {
        "operation_id": uuid4().hex,
        "plan_id": plan["id"],
        "name": "Проверка перезапуска импорта",
        "working_directory": None,
        "edges": [
            {key: edge[key] for key in ("source", "target", "explanation")}
            for edge in plan["edges"]
        ],
        "decisions": [],
    }
    workspace = api(f"{base_url}/api/github/imports", payload)
    return workspace, payload


def snapshot(base_url):
    workspaces = [
        api(f"{base_url}/api/workspaces/{item['id']}")
        for item in api(f"{base_url}/api/workspaces")
    ]
    chats = {
        task["id"]: api(
            f"{base_url}/api/workspaces/{workspace['id']}/tasklets/{task['id']}/messages"
        )
        for workspace in workspaces
        for task in workspace["tasklets"]
    }
    return {"workspaces": workspaces, "chats": chats}


def main():
    processes = []
    backend_port, github_port, provider_port = free_port(), free_port(), free_port()
    with tempfile.TemporaryDirectory(prefix="aispace-github-e2e-") as directory:
        base_url = f"http://127.0.0.1:{backend_port}"
        github_url = f"http://127.0.0.1:{github_port}"
        provider_url = f"http://127.0.0.1:{provider_port}"
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
            "AISPACE_E2E_PROVIDER_URL": provider_url,
            "AISPACE_E2E_PROVIDER_BASE_URL": f"{provider_url}/v1",
            "AISPACE_WORKSPACE_ROOT": str(ROOT / "e2e" / "fixtures" / "projects"),
            "AISPACE_CODEX_HOME": str(Path(directory) / "codex"),
            "AISPACE_CODEX_COMMAND_JSON": json.dumps(
                [sys.executable, str(ROOT / "e2e" / "fake_codex.py")]
            ),
            "AISPACE_FAKE_CODEX_AUDIT_URL": f"{provider_url}/codex/audit",
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
                (
                    [
                        sys.executable,
                        "e2e/mock_provider.py",
                        "--port",
                        str(provider_port),
                    ],
                    f"{provider_url}/v1/models",
                ),
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
            result = subprocess.run(
                [
                    "npx",
                    "playwright",
                    "test",
                    "--config",
                    "e2e/playwright.config.ts",
                    "github.*\\.spec\\.ts",
                    *sys.argv[1:],
                ],
                cwd=ROOT,
                env=environment,
                check=False,
            ).returncode
            if result:
                return result
            imported, payload = restart_fixture(base_url, github_url, provider_url)
            before = snapshot(base_url)
            backend = processes[-1]
            os.killpg(backend.pid, signal.SIGTERM)
            backend.wait(timeout=10)
            restarted = subprocess.Popen(
                command, cwd=ROOT, env=environment, start_new_session=True
            )
            processes.append(restarted)
            ready(f"{base_url}/api/health", restarted)
            if snapshot(base_url) != before:
                raise RuntimeError(
                    "Граф, источники или чаты изменились после перезапуска"
                )
            if api(f"{base_url}/api/github/imports", payload)["id"] != imported["id"]:
                raise RuntimeError("Повтор импорта после перезапуска создал копию")
            print(
                "GitHub import restart: graph, sources, chats and idempotent retry preserved",
                flush=True,
            )
            return 0
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
