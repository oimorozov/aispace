import ctypes
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "settings",
    "tasklets",
    "edges",
    "runs",
    "messages",
    "state",
    "codex_sessions",
)
PATH_COLUMNS = {
    "tasklets": {"working_directory"},
    "codex_sessions": {"cwd"},
    "threads": {"cwd", "rollout_path", "agent_path"},
    "rollout_migration_skipped_rollouts": {"rollout_path"},
    "project_roots": {"path"},
}
SOURCE_AUDIT = """import json, sqlite3
connection = sqlite3.connect('file:/data/aispace.sqlite3?mode=ro', uri=True)
pipeline = json.loads(connection.execute('SELECT pipeline FROM state WHERE id=1').fetchone()[0])
counts = {table: connection.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] for table in ('settings','tasklets','edges','runs','messages','state','codex_sessions')}
active = connection.execute("SELECT COUNT(*) FROM tasklets WHERE status IN ('running','queued')").fetchone()[0]
print(json.dumps({'counts': counts, 'active': active, 'pipeline_status': pipeline['status']}))
connection.close()
"""


def command(arguments):
    result = subprocess.run(
        arguments, cwd=ROOT, capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(f"Command failed: {arguments[0]}")
    return result.stdout.strip()


def readonly(path):
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def map_path(value, mappings):
    if value is None:
        return None
    for source, target in mappings:
        if value == source or value.startswith(f"{source}/"):
            return f"{target.rstrip('/')}{value[len(source) :]}" or "/"
    return value


def quote(value):
    return '"' + value.replace('"', '""') + '"'


def snapshot(path, ignore_paths=False):
    connection = readonly(path)
    try:
        digest = hashlib.sha256()
        for table, schema in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            cursor = connection.execute(f"SELECT * FROM {quote(table)}")
            columns = [item[0] for item in cursor.description]
            rows = []
            for row in cursor:
                values = dict(zip(columns, row))
                if ignore_paths:
                    for column in PATH_COLUMNS.get(table, set()):
                        values.pop(column, None)
                    if table == "settings":
                        settings = json.loads(values["data"])
                        settings.pop("working_directory", None)
                        values["data"] = json.dumps(settings, sort_keys=True)
                rows.append(repr(values))
            rows.sort()
            digest.update(repr((table, schema, rows)).encode())
        return digest.hexdigest()
    finally:
        connection.close()


def normalize_database(path):
    expected = snapshot(path)
    temporary = path.with_name(f"{path.name}.backup")
    source = readonly(path)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        if target.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
            raise RuntimeError("SQLite backup did not leave WAL mode")
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite backup failed integrity validation")
    finally:
        target.close()
        source.close()
    if snapshot(temporary) != expected:
        raise RuntimeError("SQLite backup changed stored records")
    temporary.replace(path)
    for suffix in ("-wal", "-shm", "-journal"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
        temporary.with_name(temporary.name + suffix).unlink(missing_ok=True)
    path.chmod(0o600)


def file_fingerprints(root, databases):
    excluded = {
        str(path.relative_to(root)) + suffix
        for path in databases
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    result = {}
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        if relative in excluded:
            continue
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            result[relative] = digest.hexdigest()
    return result


def update_column(connection, table, column, mappings):
    rows = list(
        connection.execute(f"SELECT DISTINCT {quote(column)} FROM {quote(table)}")
    )
    for (value,) in rows:
        mapped = map_path(value, mappings)
        if mapped != value:
            connection.execute(
                f"UPDATE {quote(table)} SET {quote(column)}=? WHERE {quote(column)}=?",
                (mapped, value),
            )


def migrate_paths(main_database, state_database, mappings):
    connection = sqlite3.connect(main_database)
    try:
        with connection:
            update_column(connection, "tasklets", "working_directory", mappings)
            update_column(connection, "codex_sessions", "cwd", mappings)
            raw = connection.execute("SELECT data FROM settings WHERE id=1").fetchone()[
                0
            ]
            settings = json.loads(raw)
            previous = settings.get("working_directory")
            mapped = map_path(previous, mappings)
            if mapped != previous:
                settings["working_directory"] = mapped
                connection.execute(
                    "UPDATE settings SET data=? WHERE id=1", (json.dumps(settings),)
                )
    finally:
        connection.close()
    if state_database:
        connection = sqlite3.connect(state_database)
        try:
            with connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for table, column in (
                    ("threads", "cwd"),
                    ("threads", "rollout_path"),
                    ("threads", "agent_path"),
                    ("rollout_migration_skipped_rollouts", "rollout_path"),
                    ("project_roots", "path"),
                ):
                    if table in tables:
                        update_column(connection, table, column, mappings)
        finally:
            connection.close()


def audit_main(path):
    connection = readonly(path)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in TABLES
        }
        pipeline = json.loads(
            connection.execute("SELECT pipeline FROM state WHERE id=1").fetchone()[0]
        )
        active = connection.execute(
            "SELECT COUNT(*) FROM tasklets WHERE status IN ('running','queued')"
        ).fetchone()[0]
        return {
            "counts": counts,
            "active": active,
            "pipeline_status": pipeline["status"],
        }
    finally:
        connection.close()


def require_idle(audit):
    if audit["active"] or audit["pipeline_status"] in {"running", "stopping"}:
        raise RuntimeError("Pipeline is active. Stop it in aispace before migrating.")


def validate_paths(staging, destination, state_database):
    main = readonly(staging / "aispace.sqlite3")
    try:
        settings = json.loads(
            main.execute("SELECT data FROM settings WHERE id=1").fetchone()[0]
        )
        directories = [settings.get("working_directory")]
        directories.extend(
            row[0] for row in main.execute("SELECT working_directory FROM tasklets")
        )
        sessions = list(main.execute("SELECT thread_id,cwd FROM codex_sessions"))
        directories.extend(row[1] for row in sessions)
        if any(value and not Path(value).is_dir() for value in directories):
            raise RuntimeError(
                "A migrated working directory is unavailable on this Mac"
            )
        if sessions and not state_database:
            raise RuntimeError("Codex session metadata is missing")
        if state_database:
            state = readonly(state_database)
            try:
                threads = {
                    row[0]: row[1:]
                    for row in state.execute("SELECT id,cwd,rollout_path FROM threads")
                }
                for thread_id, cwd in sessions:
                    if thread_id not in threads or threads[thread_id][0] != cwd:
                        raise RuntimeError(
                            "Codex thread IDs or working directories do not match"
                        )
                for cwd, rollout in threads.values():
                    if not Path(cwd).is_dir():
                        raise RuntimeError(
                            "A Codex working directory is unavailable on this Mac"
                        )
                    try:
                        relative = Path(rollout).relative_to(destination / "codex")
                    except ValueError as error:
                        raise RuntimeError(
                            "A Codex rollout is outside the migrated home"
                        ) from error
                    if not (staging / "codex" / relative).is_file():
                        raise RuntimeError("A Codex session rollout is missing")
            finally:
                state.close()
    finally:
        main.close()


def prepare_staging(staging, destination, workspace_source):
    main_database = staging / "aispace.sqlite3"
    databases = [main_database, *sorted((staging / "codex").glob("*.sqlite"))]
    files = file_fingerprints(staging, databases)
    before = audit_main(main_database)
    require_idle(before)
    for database in databases:
        try:
            normalize_database(database)
        except sqlite3.Error as error:
            raise RuntimeError(
                f"Unable to normalize {database.name}: {error}"
            ) from error
    records = {path: snapshot(path, ignore_paths=True) for path in databases}
    states = list((staging / "codex").glob("state_*.sqlite"))
    if len(states) > 1:
        raise RuntimeError("Multiple Codex state databases require manual review")
    state_database = states[0] if states else None
    mappings = [
        ("/data/codex", str(destination / "codex")),
        ("/workspace", str(workspace_source)),
        ("/host", "/"),
    ]
    migrate_paths(main_database, state_database, mappings)
    after = audit_main(main_database)
    if after != before:
        raise RuntimeError("Tasklet, run or message counts changed during migration")
    for database in databases:
        if snapshot(database, ignore_paths=True) != records[database]:
            raise RuntimeError(
                "Migration changed records beyond working-directory metadata"
            )
        connection = readonly(database)
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError(
                    "Migrated SQLite database failed integrity validation"
                )
        finally:
            connection.close()
    validate_paths(staging, destination, state_database)
    if file_fingerprints(staging, databases) != files:
        raise RuntimeError("Session history or credentials changed during migration")
    for path in staging.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)
    staging.chmod(0o700)
    return after["counts"]


def publish_staging(staging, destination):
    if os.path.lexists(destination):
        raise RuntimeError("Native .data already exists; refusing to overwrite it")
    if sys.platform != "darwin":
        raise RuntimeError("This migration targets macOS")
    library = ctypes.CDLL(None, use_errno=True)
    rename = library.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(os.fsencode(staging), os.fsencode(destination), 4):
        number = ctypes.get_errno()
        raise OSError(number, os.strerror(number))


def main():
    os.umask(0o077)
    destination = ROOT / ".data"
    staging = None
    try:
        if sys.platform != "darwin":
            raise RuntimeError("This migration targets macOS")
        if os.path.lexists(destination):
            raise RuntimeError("Native .data already exists; refusing to overwrite it")
        container = command(["docker", "compose", "ps", "--quiet", "backend"])
        if not container or "\n" in container:
            raise RuntimeError("Exactly one running Docker backend is required")
        mounts = json.loads(
            command(["docker", "inspect", container, "--format", "{{json .Mounts}}"])
        )
        data_mount = next(
            (item for item in mounts if item["Destination"] == "/data"), {}
        )
        host_mount = next(
            (item for item in mounts if item["Destination"] == "/host"), {}
        )
        if data_mount.get("Name") != "aispace_aispace-data":
            raise RuntimeError("The backend uses an unexpected data volume")
        if host_mount.get("Source") != "/mnt/mac":
            raise RuntimeError(
                "The Docker host mount requires a different path mapping"
            )
        workspace_source = next(
            (item["Source"] for item in mounts if item["Destination"] == "/workspace"),
            None,
        )
        if not workspace_source or not Path(workspace_source).is_dir():
            raise RuntimeError(
                "Docker workspace mount does not resolve to a Mac directory"
            )
        require_idle(
            json.loads(
                command(["docker", "exec", container, "python", "-c", SOURCE_AUDIT])
            )
        )
        command(["docker", "compose", "stop", "frontend"])
        original = json.loads(
            command(["docker", "exec", container, "python", "-c", SOURCE_AUDIT])
        )
        require_idle(original)
        command(["docker", "compose", "stop", "backend"])
        staging = Path(tempfile.mkdtemp(prefix=".data.import-", dir=ROOT))
        command(["docker", "cp", f"{container}:/data/.", str(staging)])
        if audit_main(staging / "aispace.sqlite3") != original:
            raise RuntimeError(
                "Docker state changed while stopping; copied data needs review"
            )
        counts = prepare_staging(staging, destination, Path(workspace_source))
        if os.path.lexists(destination):
            raise RuntimeError(
                "Native .data appeared during migration; refusing to overwrite it"
            )
        publish_staging(staging, destination)
        print(
            json.dumps(
                {"migrated": True, "counts": counts, "docker_volume_preserved": True}
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"Migration stopped: {error}", file=sys.stderr)
        if staging:
            print(
                "Copied data is preserved in the .data.import-* staging directory.",
                file=sys.stderr,
            )
        print("The original Docker volume is unchanged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
