import json
import sqlite3

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from aispace.models import Pipeline, SettingsPatch, Workspace, WorkspacePatch, WorkspaceSummary
from aispace.storage import Store

TABLES = ("tasklets", "edges", "messages", "runs", "codex_sessions")


def rows(connection, table):
    return [dict(row) for row in connection.execute(f"SELECT rowid,* FROM {table} ORDER BY rowid")]


def legacy_database(directory, own_cwd=True, running=False):
    directory.mkdir()
    connection = sqlite3.connect(directory / "aispace.sqlite3")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE settings (id INTEGER PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE tasklets (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, prompt TEXT NOT NULL,
            model TEXT, status TEXT NOT NULL, position TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error TEXT, last_output TEXT NOT NULL
        );
        CREATE TABLE edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL REFERENCES tasklets(id) ON DELETE CASCADE,
            target TEXT NOT NULL REFERENCES tasklets(id) ON DELETE CASCADE,
            pass_context INTEGER NOT NULL DEFAULT 0, UNIQUE(source,target)
        );
        CREATE TABLE runs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            tasklet_id TEXT NOT NULL REFERENCES tasklets(id) ON DELETE CASCADE,
            role TEXT NOT NULL, content TEXT NOT NULL,
            created_at TEXT NOT NULL, run_id TEXT REFERENCES runs(id)
        );
        CREATE INDEX messages_tasklet ON messages(tasklet_id,created_at);
        CREATE TABLE state (id INTEGER PRIMARY KEY, pipeline TEXT NOT NULL);
        CREATE TABLE codex_sessions (
            tasklet_id TEXT PRIMARY KEY REFERENCES tasklets(id) ON DELETE CASCADE,
            thread_id TEXT NOT NULL, cwd TEXT NOT NULL
        );
        """
    )
    settings = {
        "api_key": "test-secret-preserved",
        "execution_mode": "codex",
        "base_url": "https://example.com/v1",
        "model": "custom-model",
        "max_parallel": 2,
        "codex_sandbox": "workspace-write",
        "workspace_context": "Контекст проекта\nВторая строка",
        "working_directory": "/projects/legacy",
    }
    connection.execute("INSERT INTO settings VALUES (1,?)", (json.dumps(settings),))
    for tasklet_id, status, position, error in (
        ("task-a", "running" if running else "completed", {"x": 52.5, "y": -8}, None),
        ("task-b", "queued" if running else "failed", {"x": -15, "y": 36}, "known error"),
    ):
        connection.execute(
            "INSERT INTO tasklets VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                tasklet_id, f"Название {tasklet_id}", f"Prompt {tasklet_id}", "own-model", status,
                json.dumps(position), "2026-01-01", "2026-02-01", error, f"Output {tasklet_id}",
            ),
        )
    if own_cwd:
        connection.execute("ALTER TABLE tasklets ADD COLUMN working_directory TEXT")
        connection.execute(
            "UPDATE tasklets SET working_directory='/projects/override' WHERE id='task-a'"
        )
    connection.execute("INSERT INTO edges VALUES ('edge-a','task-a','task-b',1)")
    last_pipeline = Pipeline(
        id="run-last", status="running" if running else "completed",
        started_at="2026-01-02", finished_at=None if running else "2026-01-03",
        total=2, completed=0 if running else 2,
    ).model_dump()
    old_pipeline = Pipeline(id="run-old", status="failed", error="earlier failure").model_dump()
    connection.executemany(
        "INSERT INTO runs VALUES (?,?)",
        [(item["id"], json.dumps(item)) for item in (old_pipeline, last_pipeline)],
    )
    connection.execute("INSERT INTO state VALUES (1,?)", (json.dumps(last_pipeline),))
    connection.executemany(
        "INSERT INTO messages (rowid,id,tasklet_id,role,content,created_at,run_id) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            (3, "message-z", "task-a", "user", "First", "2026-02-01", "run-old"),
            (9, "message-a", "task-a", "assistant", "Second", "2026-02-01", "run-last"),
            (14, "message-b", "task-b", "user", "Third", "2026-02-03", None),
        ],
    )
    connection.executemany(
        "INSERT INTO codex_sessions VALUES (?,?,?)",
        [("task-a", "thread-existing-a", "/projects/override"),
         ("task-b", "thread-existing-b", "/projects/legacy")],
    )
    connection.commit()
    snapshot = {table: rows(connection, table) for table in TABLES}
    snapshot.update(settings=settings, pipeline=last_pipeline)
    connection.close()
    return snapshot


def seed_workspace(store, workspace_id, label):
    store.update_workspace(
        workspace_id,
        {"name": label, "workspace_context": f"Context {label}", "working_directory": f"/{label}"},
    )
    first = store.create_tasklet({"title": f"{label}-first"}, workspace_id)
    second = store.create_tasklet({"title": f"{label}-second"}, workspace_id)
    edge = store.create_edge({"source": first["id"], "target": second["id"]}, workspace_id)
    pipeline = Pipeline(id=f"run-{label}", status="completed", total=1, completed=1).model_dump()
    store.save_pipeline(pipeline, workspace_id)
    message = store.create_message(first["id"], "assistant", f"Output {label}", pipeline["id"], workspace_id)
    store.save_codex_session(first["id"], f"thread-{label}", f"/{label}", workspace_id)
    return first, second, edge, message, pipeline


@pytest.mark.parametrize("own_cwd", [True, False])
def test_legacy_migration_preserves_data_and_is_idempotent(tmp_path, own_cwd):
    directory = tmp_path / "data"
    before = legacy_database(directory, own_cwd)
    auth = directory / "auth-control.json"
    auth.write_text("unrelated auth state")
    store = Store(directory)
    workspace_id = store.default_workspace_id
    assert len(store.workspaces()) == 1
    for table in TABLES:
        migrated = rows(store.connection, table)
        assert {row.pop("workspace_id") for row in migrated} == {workspace_id}
        expected = before[table]
        if table == "tasklets" and not own_cwd:
            expected = [{**row, "working_directory": None} for row in expected]
        if table == "tasklets":
            expected = [{**row, "conversation_id": None} for row in expected]
        assert migrated == expected
    workspace = store.workspace(workspace_id)
    assert workspace["workspace_context"] == before["settings"]["workspace_context"]
    assert workspace["working_directory"] == before["settings"]["working_directory"]
    assert workspace["pipeline"] == before["pipeline"]
    assert store.settings(private=True, workspace_id=workspace_id) == before["settings"]
    assert "workspace_context" not in store.settings(private=True)
    assert "working_directory" not in store.settings(private=True)
    assert "api_key" not in store.settings()
    assert store.settings()["api_key_configured"] is True
    assert [message["id"] for message in store.messages("task-a")] == ["message-z", "message-a"]
    assert store.codex_session("task-a") == {
        "thread_id": "thread-existing-a", "cwd": "/projects/override",
    }
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not store.connection.execute("PRAGMA foreign_key_check").fetchall()
    assert auth.read_text() == "unrelated auth state"
    store.close()
    reopened = Store(directory)
    assert reopened.default_workspace_id == workspace_id
    assert reopened.workspace() == workspace
    assert len(reopened.workspaces()) == 1
    reopened.close()


def test_migration_failure_rolls_back_schema_and_all_data_then_retries(tmp_path):
    directory = tmp_path / "data"
    legacy_database(directory, own_cwd=False)
    with sqlite3.connect(directory / "aispace.sqlite3") as connection:
        before = list(connection.iterdump())

    class FailingStore(Store):
        def _migrate_workspaces(self):
            super()._migrate_workspaces()
            raise RuntimeError("injected after table replacement")

    with pytest.raises(RuntimeError, match="injected"):
        FailingStore(directory)
    with sqlite3.connect(directory / "aispace.sqlite3") as connection:
        assert list(connection.iterdump()) == before
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='workspaces'"
        ).fetchall()
    store = Store(directory)
    assert len(store.workspaces()) == 1
    assert len(store.tasklets()) == 2
    store.close()


def test_migration_preserves_running_state_then_startup_recovery_cancels(tmp_path):
    directory = tmp_path / "data"
    before = legacy_database(directory, running=True)

    class WithoutRecovery(Store):
        def recover(self):
            pass

    store = WithoutRecovery(directory)
    assert store.pipeline() == before["pipeline"]
    assert [item["status"] for item in store.tasklets()] == ["running", "queued"]
    messages = store.messages("task-a")
    session = store.codex_session("task-a")
    store.close()
    store = Store(directory)
    assert store.pipeline()["status"] == "cancelled"
    assert store.pipeline()["id"] == "run-last"
    assert [item["status"] for item in store.tasklets()] == ["cancelled", "cancelled"]
    assert store.runs()[0] == json.loads(before["runs"][0]["data"])
    assert store.messages("task-a") == messages
    assert store.codex_session("task-a") == session
    store.close()


def test_new_workspace_empty_settings_scoped_and_summaries_lightweight(tmp_path):
    store = Store(tmp_path)
    first = store.default_workspace_id
    seed_workspace(store, first, "A")
    store.save_settings({"api_key": "shared-test-key", "model": "shared-model"})
    second = store.create_workspace({"name": "B"})
    assert second["workspace_context"] == ""
    assert second["working_directory"] is None
    assert second["tasklets"] == second["edges"] == []
    assert second["pipeline"] == Pipeline().model_dump()
    assert store.settings(True, first)["workspace_context"] == "Context A"
    assert store.settings(True, second["id"])["workspace_context"] == ""
    assert store.settings(True, second["id"])["api_key"] == "shared-test-key"
    assert store.settings(True, second["id"])["model"] == "shared-model"
    assert set(store.workspaces()[0]) == {"id", "name", "created_at", "updated_at", "pipeline"}
    Workspace.model_validate(second)
    WorkspaceSummary.model_validate(store.workspaces()[0])
    with pytest.raises(ValueError):
        store.save_settings({"workspace_context": "wrong scope"})
    with pytest.raises(ValidationError):
        SettingsPatch(workspace_context="wrong scope")
    with pytest.raises(ValidationError):
        WorkspacePatch(name=None)
    store.close()


def test_foreign_entities_and_run_ids_are_rejected_without_changes(tmp_path):
    store = Store(tmp_path)
    first = store.default_workspace_id
    second = store.create_workspace({"name": "B"})["id"]
    task_a, _, edge_a, message_a, run_a = seed_workspace(store, first, "A")
    task_b, _, _, _, _ = seed_workspace(store, second, "B")
    snapshot = {table: rows(store.connection, table) for table in TABLES}
    spaces = store.workspaces()
    assert store.tasklet(task_a["id"], second) is None
    assert {task["workspace_id"] for task in store.tasklets()} == {first}
    operations = [
        lambda: store.update_tasklet(task_a["id"], {"title": "wrong"}, second),
        lambda: store.delete_tasklet(task_a["id"], second),
        lambda: store.messages(task_a["id"], second),
        lambda: store.create_message(task_a["id"], "user", "wrong", None, second),
        lambda: store.create_message(task_b["id"], "user", "wrong", run_a["id"], second),
        lambda: store.update_message(message_a, "wrong", second),
        lambda: store.update_message({**message_a, "tasklet_id": task_b["id"]}, "wrong", second),
        lambda: store.update_edge(edge_a["id"], True, second),
        lambda: store.delete_edge(edge_a["id"], second),
        lambda: store.create_edge({"source": task_a["id"], "target": task_b["id"]}, first),
        lambda: store.codex_session(task_a["id"], second),
        lambda: store.save_codex_session(task_a["id"], "wrong", "/wrong", second),
        lambda: store.save_pipeline(run_a, second),
    ]
    for operation in operations:
        with pytest.raises(HTTPException) as error:
            operation()
        assert error.value.status_code == 404
        assert {table: rows(store.connection, table) for table in TABLES} == snapshot
        assert store.workspaces() == spaces
    assert store.codex_session(task_b["id"])["thread_id"] == "thread-B"
    store.close()


def test_database_foreign_keys_prevent_cross_workspace_edges_and_messages(tmp_path):
    store = Store(tmp_path)
    first = store.default_workspace_id
    second = store.create_workspace({"name": "B"})["id"]
    task_a, _, _, _, run_a = seed_workspace(store, first, "A")
    task_b, _, _, _, _ = seed_workspace(store, second, "B")
    with pytest.raises(sqlite3.IntegrityError), store.connection:
        store.connection.execute(
            "INSERT INTO edges (id,workspace_id,source,target,pass_context) VALUES ('wrong',?,?,?,0)",
            (first, task_a["id"], task_b["id"]),
        )
    with pytest.raises(sqlite3.IntegrityError), store.connection:
        store.connection.execute(
            "INSERT INTO messages (id,workspace_id,tasklet_id,role,content,created_at,run_id) "
            "VALUES ('wrong',?,?,'user','wrong','today',?)",
            (second, task_b["id"], run_a["id"]),
        )
    assert not store.connection.execute("PRAGMA foreign_key_check").fetchall()
    store.close()


def test_pipeline_update_keeps_messages_and_run_history_scoped(tmp_path):
    store = Store(tmp_path)
    first = store.default_workspace_id
    second = store.create_workspace({"name": "B"})["id"]
    task_a, _, _, message_a, run_a = seed_workspace(store, first, "A")
    _, _, _, _, run_b = seed_workspace(store, second, "B")
    store.save_pipeline({**run_a, "status": "failed", "error": "later error"}, first)
    assert store.messages(task_a["id"], first) == [message_a]
    assert len(store.runs(first)) == 1
    assert store.runs(first)[0]["error"] == "later error"
    assert store.runs(second) == [run_b]
    store.close()


def test_delete_cascades_only_owner_preserves_files_globals_and_zero_on_restart(tmp_path):
    store = Store(tmp_path / "data")
    first = store.default_workspace_id
    second = store.create_workspace({"name": "B"})["id"]
    seed_workspace(store, first, "A")
    seed_workspace(store, second, "B")
    controls = [tmp_path / "project-A", tmp_path / "project-B"]
    for workspace_id, control in zip((first, second), controls, strict=True):
        control.mkdir()
        (control / "keep.txt").write_text("project files stay")
        store.update_workspace(workspace_id, {"working_directory": str(control)})
    store.save_settings({"api_key": "shared-key"})
    global_settings = store.settings(True)
    snapshot_b = store.workspace(second)
    assert store.delete_workspace(first)
    assert store.workspace(first) is None
    assert store.workspace(second) == snapshot_b
    for table in TABLES:
        assert all(row["workspace_id"] == second for row in rows(store.connection, table))
    assert store.settings(True) == global_settings
    assert all((control / "keep.txt").read_text() == "project files stay" for control in controls)
    assert store.delete_workspace(second)
    assert not store.delete_workspace(second)
    assert store.default_workspace_id is None
    assert store.workspace() is None
    assert store.workspaces() == []
    store.close()
    store = Store(tmp_path / "data")
    assert store.workspaces() == []
    assert store.settings(True) == global_settings
    with pytest.raises(HTTPException) as error:
        store.create_tasklet({"title": "no owner"})
    assert error.value.status_code == 404
    assert store.create_workspace({"name": "New"})["tasklets"] == []
    assert len(store.workspaces()) == 1
    store.close()


@pytest.mark.parametrize("status", ["running", "stopping"])
def test_delete_active_workspace_rejected_but_other_workspace_deletable(tmp_path, status):
    store = Store(tmp_path)
    first = store.default_workspace_id
    second = store.create_workspace({"name": "B"})["id"]
    seed_workspace(store, first, "A")
    pipeline = {**store.pipeline(first), "status": status}
    store.save_pipeline(pipeline, first)
    with pytest.raises(HTTPException) as error:
        store.delete_workspace(first)
    assert error.value.status_code == 409
    assert store.delete_workspace(second)
    assert store.pipeline(first) == pipeline
    assert len(store.tasklets(first)) == 2
    store.close()


def test_cascaded_delete_failure_rolls_back_all_owned_records(tmp_path):
    store = Store(tmp_path)
    workspace_id = store.default_workspace_id
    seed_workspace(store, workspace_id, "A")
    before = {table: rows(store.connection, table) for table in (*TABLES, "workspaces", "settings")}
    store.connection.execute(
        "CREATE TRIGGER fail_deletion BEFORE DELETE ON tasklets "
        "BEGIN SELECT RAISE(ABORT,'injected delete failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected delete failure"):
        store.delete_workspace(workspace_id)
    after = {table: rows(store.connection, table) for table in (*TABLES, "workspaces", "settings")}
    assert after == before
    store.connection.execute("DROP TRIGGER fail_deletion")
    assert store.delete_workspace(workspace_id)
    store.close()


async def test_migrated_codex_session_resumes_original_thread(tmp_path, monkeypatch):
    import sys

    from test_codex import FAKE, Directories

    from aispace.codex import CodexProvider

    directory = tmp_path / "data"
    legacy_database(directory)
    cwd = str(tmp_path.resolve())
    with sqlite3.connect(directory / "aispace.sqlite3") as connection:
        connection.execute(
            "UPDATE tasklets SET working_directory=? WHERE id='task-a'", (cwd,)
        )
        connection.execute(
            "UPDATE codex_sessions SET cwd=? WHERE tasklet_id='task-a'", (cwd,)
        )
    script = tmp_path / "fake-codex.py"
    script.write_text(FAKE)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AISPACE_CODEX_COMMAND_JSON", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(audit))
    monkeypatch.setenv("FAKE_CODEX_MODE", "complete")
    store = Store(directory)
    workspace_id = store.default_workspace_id
    provider = CodexProvider(store, Directories())
    original = store.codex_session("task-a", workspace_id)
    try:
        output = "".join(
            [
                chunk async for chunk in provider.stream(
                    store.settings(True, workspace_id), "",
                    [{"role": "user", "content": "Continue migrated conversation"}],
                    store.tasklet("task-a", workspace_id),
                )
            ]
        )
        assert output == "answer"
        requests = [json.loads(line) for line in audit.read_text().splitlines()]
        resumes = [item for item in requests if item.get("method") == "thread/resume"]
        assert len(resumes) == 1
        assert resumes[0]["params"]["threadId"] == "thread-existing-a"
        assert resumes[0]["params"]["cwd"] == cwd
        assert not any(item.get("method") == "thread/start" for item in requests)
        assert store.codex_session("task-a", workspace_id) == original
    finally:
        await provider.close()
        store.close()
