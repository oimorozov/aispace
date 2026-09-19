import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aispace.models import Pipeline, TaskletCreate
from aispace.storage import Store

spec = importlib.util.spec_from_file_location(
    "migrate_docker", Path(__file__).resolve().parents[2] / "scripts" / "migrate_docker.py"
)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("/host", "/"),
        ("/host/Users/alice/project", "/Users/alice/project"),
        ("/hosted/project", "/hosted/project"),
        ("/workspace", "/Users/alice/project"),
        ("/workspace/sub", "/Users/alice/project/sub"),
        ("/workspace-other", "/workspace-other"),
        ("/data/codex/sessions/turn.jsonl", "/native/.data/codex/sessions/turn.jsonl"),
        ("/data/codex-old/session", "/data/codex-old/session"),
    ],
)
def test_path_mapping_respects_directory_boundaries(value, expected):
    mappings = [
        ("/data/codex", "/native/.data/codex"),
        ("/workspace", "/Users/alice/project"),
        ("/host", "/"),
    ]
    assert migration.map_path(value, mappings) == expected


def create_fixture(tmp_path):
    staging = tmp_path / ".data.import-test"
    destination = tmp_path / ".data"
    workspace = tmp_path / "project"
    component = workspace / "component"
    component.mkdir(parents=True)
    store = Store(staging)
    store.save_settings(
        {
            "execution_mode": "codex",
            "working_directory": "/workspace",
            "api_key": "private-fixture-key",
        }
    )
    run = Pipeline(id="run-one", status="completed", total=2, completed=2).model_dump()
    store.save_pipeline(run)
    pairs = []
    for index, cwd in enumerate((f"/host{workspace}", "/workspace/component")):
        tasklet = store.create_tasklet(
            TaskletCreate(
                title=f"Task {index}", prompt="Original prompt", working_directory=cwd
            ).model_dump()
        )
        thread_id = f"thread-{index}"
        store.save_codex_session(tasklet["id"], thread_id, cwd)
        store.update_tasklet(tasklet["id"], {"status": "completed"})
        for role, content in (
            ("user", "Private original request"),
            ("assistant", "Original result"),
        ):
            store.create_message(tasklet["id"], role, content, run["id"])
        pairs.append((tasklet["id"], thread_id, cwd))
    store.create_edge(
        {"id": "edge-one", "source": pairs[0][0], "target": pairs[1][0], "pass_context": False}
    )
    store.close()
    codex = staging / "codex"
    sessions = codex / "sessions"
    sessions.mkdir(parents=True)
    (codex / "auth.json").write_text('{"token":"private-fixture-token"}\n')
    source = tmp_path / "source-state.sqlite"
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "CREATE TABLE threads (id TEXT PRIMARY KEY,cwd TEXT,rollout_path TEXT,agent_path TEXT,title TEXT)"
    )
    connection.execute("CREATE TABLE project_roots (id TEXT PRIMARY KEY,path TEXT) WITHOUT ROWID")
    connection.execute("INSERT INTO project_roots VALUES ('root','/workspace')")
    for _, thread_id, cwd in pairs:
        (sessions / f"{thread_id}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": {"id": thread_id, "cwd": cwd}}) + "\n"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?,?,?,?,?)",
            (thread_id, cwd, f"/data/codex/sessions/{thread_id}.jsonl", "", "Original title"),
        )
    connection.commit()
    for suffix in ("", "-wal", "-shm"):
        shutil.copy2(str(source) + suffix, codex / f"state_5.sqlite{suffix}")
    connection.close()
    return staging, destination, workspace, pairs


def test_migration_preserves_wal_records_messages_credentials_and_rollouts(tmp_path):
    staging, destination, workspace, pairs = create_fixture(tmp_path)
    codex = staging / "codex"
    assert (codex / "state_5.sqlite-wal").stat().st_size > 0
    originals = {path.name: path.read_bytes() for path in (codex / "sessions").glob("*.jsonl")}
    credentials = (codex / "auth.json").read_bytes()
    counts = migration.prepare_staging(staging, destination, workspace)
    assert counts == {
        "settings": 1,
        "tasklets": 2,
        "edges": 1,
        "runs": 1,
        "messages": 4,
        "state": 1,
        "codex_sessions": 2,
    }
    connection = migration.readonly(staging / "aispace.sqlite3")
    try:
        settings = json.loads(connection.execute("SELECT data FROM settings").fetchone()[0])
        assert settings["working_directory"] == str(workspace)
        assert settings["api_key"] == "private-fixture-key"
        assert list(
            connection.execute("SELECT thread_id,cwd FROM codex_sessions ORDER BY thread_id")
        ) == [("thread-0", str(workspace)), ("thread-1", str(workspace / "component"))]
        assert {row[0] for row in connection.execute("SELECT id FROM tasklets")} == {
            pair[0] for pair in pairs
        }
        assert list(
            connection.execute("SELECT DISTINCT content FROM messages ORDER BY content")
        ) == [("Original result",), ("Private original request",)]
    finally:
        connection.close()
    state = migration.readonly(codex / "state_5.sqlite")
    try:
        assert state.execute("SELECT path FROM project_roots").fetchone()[0] == str(workspace)
        assert state.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert list(state.execute("SELECT rollout_path FROM threads ORDER BY id")) == [
            (str(destination / "codex" / "sessions" / f"thread-{index}.jsonl"),)
            for index in range(2)
        ]
    finally:
        state.close()
    assert (codex / "auth.json").read_bytes() == credentials
    assert {
        path.name: path.read_bytes() for path in (codex / "sessions").glob("*.jsonl")
    } == originals
    assert not (codex / "state_5.sqlite-wal").exists()
    assert staging.stat().st_mode & 0o777 == 0o700
    assert (codex / "auth.json").stat().st_mode & 0o777 == 0o600
    assert not destination.exists()


def test_missing_rollout_stops_migration_before_publication(tmp_path):
    staging, destination, workspace, _ = create_fixture(tmp_path)
    (staging / "codex" / "sessions" / "thread-1.jsonl").unlink()
    with pytest.raises(RuntimeError, match="rollout is missing"):
        migration.prepare_staging(staging, destination, workspace)
    assert not destination.exists()
    assert staging.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Checks the macOS system SQLite runtime")
def test_system_python_leaves_no_temporary_backup_sidecars(tmp_path):
    staging, destination, workspace, _ = create_fixture(tmp_path)
    script = Path(migration.__file__).resolve()
    code = (
        "import sys; from pathlib import Path; "
        "sys.path.insert(0, sys.argv[1]); import migrate_docker as migration; "
        "migration.prepare_staging(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))"
    )
    subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            code,
            str(script.parent),
            str(staging),
            str(destination),
            str(workspace),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert not list(staging.rglob("*.backup*"))
    assert not destination.exists()


@pytest.mark.parametrize("directory", [True, False])
def test_existing_destination_is_never_overwritten(tmp_path, directory):
    staging = tmp_path / "staging"
    staging.mkdir()
    destination = tmp_path / ".data"
    if directory:
        destination.mkdir()
    else:
        destination.symlink_to(tmp_path / "missing")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        migration.publish_staging(staging, destination)
    assert staging.exists()
    assert os.path.lexists(destination)


@pytest.mark.skipif(sys.platform != "darwin", reason="Exclusive rename uses the macOS API")
def test_staging_is_published_atomically(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "payload").write_text("preserved")
    destination = tmp_path / ".data"
    migration.publish_staging(staging, destination)
    assert not staging.exists()
    assert (destination / "payload").read_text() == "preserved"


@pytest.mark.parametrize(
    "audit",
    [
        {"active": 1, "pipeline_status": "completed"},
        {"active": 0, "pipeline_status": "running"},
        {"active": 0, "pipeline_status": "stopping"},
    ],
)
def test_active_pipeline_is_rejected(audit):
    with pytest.raises(RuntimeError, match="Pipeline is active"):
        migration.require_idle(audit)
