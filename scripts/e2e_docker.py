import json
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from dev import ROOT, available


def api(path, method="GET", body=None):
    request = Request(
        f"http://127.0.0.1:8081/api/{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def snapshot():
    workspace = api("workspace")
    return {
        "workspace": workspace,
        "settings": api("settings"),
        "messages": {
            item["id"]: api(f"tasklets/{item['id']}/messages")
            for item in workspace["tasklets"]
        },
    }


def prepare_restart_check():
    api(
        "settings",
        "PATCH",
        {
            "execution_mode": "api",
            "model": "test-model",
            "api_key": "local-test-key",
            "base_url": "http://mock:8766/v1",
        },
    )
    first = api(
        "tasklets",
        "POST",
        {"title": "Persistence source", "prompt": "[[label:persist-source]]"},
    )
    second = api(
        "tasklets",
        "POST",
        {"title": "Persistence target", "prompt": "[[label:persist-target]]"},
    )
    api(
        "edges",
        "POST",
        {"source": first["id"], "target": second["id"], "pass_context": True},
    )
    api("pipeline/start", "POST", {"tasklet_ids": [second["id"]]})
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        pipeline = api("workspace")["pipeline"]
        if pipeline["status"] == "completed":
            if all(
                len(api(f"tasklets/{item['id']}/messages")) >= 2
                for item in (first, second)
            ):
                return
            raise RuntimeError("Pipeline completed without persisted chat history")
        if pipeline["status"] not in {"running", "stopping"}:
            raise RuntimeError("Persistence check pipeline did not complete")
        time.sleep(0.1)
    raise RuntimeError("Persistence check pipeline timed out")


def main():
    for port in (8081, 8766):
        if not available(port):
            print(
                f"Port {port} is in use. Stop the other test servers first.",
                file=sys.stderr,
            )
            return 1

    environment = {
        **os.environ,
        "AISPACE_PORT": "8081",
        "AISPACE_E2E_BASE_URL": "http://127.0.0.1:8081",
        "AISPACE_E2E_API_URL": "http://127.0.0.1:8081",
        "AISPACE_E2E_PROVIDER_URL": "http://mock:8766/v1",
    }
    compose = [
        "docker",
        "compose",
        "--project-name",
        f"aispace-e2e-{os.getpid()}",
        "-f",
        "compose.yaml",
        "-f",
        "compose.e2e.yaml",
    ]

    def run(arguments, check=True):
        return subprocess.run(arguments, cwd=ROOT, env=environment, check=check)

    try:
        run([*compose, "up", "-d", "--build", "--wait"])
        result = run(
            [
                "npx",
                "playwright",
                "test",
                "--config",
                "e2e/playwright.config.ts",
                *sys.argv[1:],
            ],
            check=False,
        )
        if result.returncode:
            run([*compose, "logs", "--no-color", "--tail", "50"], check=False)
            return result.returncode

        prepare_restart_check()
        before = snapshot()
        run([*compose, "restart", "backend", "frontend"])
        run([*compose, "up", "-d", "--wait"])
        if snapshot() != before:
            raise RuntimeError("Data changed after restarting the containers")
        if before["settings"]["api_key_configured"]:
            result = api("settings/test", "POST")
            if not result["ok"]:
                raise RuntimeError("Saved API connection did not survive restart")
        print(
            "Docker restart verified: tasklets, dependencies, chats and settings persisted."
        )
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    finally:
        run([*compose, "down", "--volumes", "--rmi", "local"], check=False)


if __name__ == "__main__":
    sys.exit(main())
