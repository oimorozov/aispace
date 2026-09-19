import errno
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def native_environment():
    environment = os.environ.copy()
    data = Path(environment.get("AISPACE_DATA_DIR", ROOT / ".data")).resolve()
    environment.setdefault("AISPACE_DATA_DIR", str(data))
    migrated_codex = data / "codex"
    if migrated_codex.is_dir():
        environment.setdefault("AISPACE_CODEX_HOME", str(migrated_codex))
    return environment


def available(port):
    with socket.socket() as connection:
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            connection.bind(("127.0.0.1", port))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            raise SystemExit(f"Cannot bind to port {port}: {error}") from error
    return True


def main():
    for port in (8000, 5173):
        if not available(port):
            print(
                f"Port {port} is in use. Stop the other server and try again.",
                file=sys.stderr,
            )
            return 1

    commands = [
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
        ["npm", "--prefix", "frontend", "run", "dev", "--", "--strictPort"],
    ]
    processes = []

    def stop(signum=None, frame=None):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    print("aispace · http://127.0.0.1:5173", flush=True)

    try:
        for command in commands:
            processes.append(
                subprocess.Popen(
                    command, cwd=ROOT, env=native_environment(), start_new_session=True
                )
            )
        while all(process.poll() is None for process in processes):
            time.sleep(0.2)
        return next(
            (
                process.returncode
                for process in processes
                if process.returncode is not None
            ),
            1,
        )
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    sys.exit(main())
