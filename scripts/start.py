import argparse
import os
import subprocess
import sys

from dev import ROOT, available, native_environment


def main():
    parser = argparse.ArgumentParser(description="Run aispace on this computer")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("AISPACE_PORT", "8080"))
    )
    parser.add_argument("--no-build", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    if not available(args.port):
        print(
            f"Port {args.port} is already in use. Stop the other aispace instance first.",
            file=sys.stderr,
        )
        return 1
    if not args.no_build:
        result = subprocess.run(
            ["npm", "--prefix", "frontend", "run", "build"], cwd=ROOT, check=False
        )
        if result.returncode:
            return result.returncode
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        print("Frontend is not built. Run npm run build first.", file=sys.stderr)
        return 1
    environment = {
        **native_environment(),
        "AISPACE_FRONTEND_DIR": str(frontend),
        "AISPACE_ALLOWED_ORIGINS": ",".join(
            f"http://{host}:{args.port}" for host in ("localhost", "127.0.0.1")
        ),
    }
    command = [
        "uv",
        "run",
        "--package",
        "aispace-backend",
        "uvicorn",
        "aispace.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(args.port),
        "--timeout-graceful-shutdown",
        "25",
    ]
    print(f"aispace · http://localhost:{args.port} · native host", flush=True)
    os.chdir(ROOT)
    os.execvpe(command[0], command, environment)


if __name__ == "__main__":
    sys.exit(main())
