import json
import sqlite3
import sys

import pytest
from fastapi import HTTPException
from test_api import FakeProvider, finished
from test_codex import FAKE
from test_workspace_context import StubCodex

from aispace.codex import CodexProvider
from aispace.directories import Directories
from aispace.models import Pipeline
from aispace.runtime import Runtime
from aispace.storage import Store


@pytest.fixture
def environment(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings({"api_key": "shared-key", "model": "test-model"})
    store.update_workspace(store.default_workspace_id, {"working_directory": str(tmp_path)})
    provider = FakeProvider()
    runtime = Runtime(store, provider, StubCodex(), Directories(tmp_path))
    yield store, runtime, provider
    store.close()


def old_task(store, title, workspace_id=None):
    task = store.create_tasklet({"title": title, "prompt": title}, workspace_id)
    store.update_tasklet(task["id"], {
        "status": "completed", "last_output": f"old result {title}", "error": "old error",
    }, workspace_id)
    store.create_message(task["id"], "user", f"old question {title}", None, workspace_id)
    store.create_message(task["id"], "assistant", f"old answer {title}", None, workspace_id)
    store.save_codex_session(task["id"], f"old-thread-{title}", "/unchanged", workspace_id)
    return task


def database(store):
    return list(store.connection.iterdump())


def conversation(store, task, workspace_id=None):
    current = store.tasklet(task["id"], workspace_id)
    return (
        store.messages(task["id"], workspace_id),
        store.codex_session(task["id"], workspace_id),
        current["last_output"],
        current["conversation_id"],
    )


async def test_full_run_resets_all_chats_before_work_and_preserves_runs(environment):
    store, runtime, provider = environment
    tasks = [old_task(store, name) for name in ("A", "B", "C")]
    old_run = Pipeline(id="earlier-run", status="completed").model_dump()
    store.save_pipeline(old_run)
    store.save_settings({"max_parallel": 1})
    store.update_workspace(store.default_workspace_id, {"workspace_context": "shared context"})
    queue = runtime.events.subscribe()
    pipeline = await runtime.start()
    assert provider.calls == []
    for task in tasks:
        assert conversation(store, task) == ([], None, "", pipeline["id"])
        assert store.tasklet(task["id"])["status"] == "queued"
        assert store.tasklet(task["id"])["error"] is None
    event, reset = queue.get_nowait()
    assert event == "chat_reset"
    assert set(reset["tasklet_ids"]) == {task["id"] for task in tasks}
    assert reset["conversation_id"] == pipeline["id"]
    assert queue.get_nowait()[0] == "workspace"
    await finished(runtime)
    assert store.runs()[0] == old_run
    assert len(store.runs()) == 2
    for call in provider.calls:
        assert call["messages"] == [
            {"role": "system", "content": "shared context"},
            {"role": "user", "content": call["label"]},
        ]
    await runtime.send_message(tasks[0]["id"], "follow-up")
    await finished(runtime)
    assert len(store.messages(tasks[0]["id"])) == 4
    assert any(message["role"] == "assistant" for message in provider.calls[-1]["messages"])
    first_generation = store.tasklet(tasks[0]["id"])["conversation_id"]
    await runtime.start()
    await finished(runtime)
    assert len(store.messages(tasks[0]["id"])) == 2
    assert store.tasklet(tasks[0]["id"])["conversation_id"] != first_generation
    assert len(store.runs()) == 4
    assert not any("follow-up" in item["content"] for call in provider.calls[-3:] for item in call["messages"])


@pytest.mark.parametrize("ancestor_completed", [True, False])
async def test_subset_resets_chosen_only_preserves_other_spaces_and_dependency_context(environment, ancestor_completed):
    store, runtime, provider = environment
    a, b, c, d = [old_task(store, name) for name in ("A", "B", "C", "D")]
    store.create_edge({"source": a["id"], "target": b["id"], "pass_context": True})
    store.create_edge({"source": b["id"], "target": d["id"]})
    other = store.create_workspace({"name": "other"})["id"]
    other_task = old_task(store, "other", other)
    other_conversation = conversation(store, other_task, other)
    untouched = {task["id"]: conversation(store, task) for task in (a, c, d)}
    if not ancestor_completed:
        store.update_tasklet(a["id"], {"status": "idle"})
    pipeline = await runtime.start([b["id"]])
    assert pipeline["total"] == (1 if ancestor_completed else 2)
    assert conversation(store, b) == ([], None, "", pipeline["id"])
    for task in (c, d, *([a] if ancestor_completed else [])):
        assert conversation(store, task) == untouched[task["id"]]
    assert store.tasklet(d["id"])["status"] == "idle"
    assert conversation(store, other_task, other) == other_conversation
    await finished(runtime)
    assert conversation(store, other_task, other) == other_conversation
    dependency = next(call for call in provider.calls if call["label"] == "B")["messages"][0]
    expected = "old result A" if ancestor_completed else "Результат A."
    assert expected in dependency["content"]


async def test_immediate_stop_keeps_new_empty_chats_for_queued_tasks(environment):
    store, runtime, provider = environment
    tasks = [old_task(store, name) for name in ("A", "B", "C")]
    store.save_settings({"max_parallel": 1})
    pipeline = await runtime.start()
    await runtime.stop()
    assert provider.calls == []
    assert store.pipeline()["status"] == "cancelled"
    for task in tasks:
        assert conversation(store, task) == ([], None, "", pipeline["id"])
        assert store.tasklet(task["id"])["status"] == "cancelled"


@pytest.mark.parametrize("reason", ["prompt", "id", "key", "model", "directory", "auth", "command", "resume", "active"])
async def test_failed_validation_never_resets_existing_data(environment, reason, tmp_path):
    store, runtime, _ = environment
    task = old_task(store, "A")
    selected = [task["id"]]
    if reason == "prompt":
        store.update_tasklet(task["id"], {"prompt": " "})
    elif reason == "id":
        selected = ["missing"]
    elif reason == "key":
        store.save_settings({"api_key": None})
    elif reason == "model":
        store.save_settings({"model": ""})
    elif reason in {"directory", "auth", "resume"}:
        store.save_settings({"execution_mode": "codex"})
        if reason == "directory":
            store.update_tasklet(task["id"], {"working_directory": str(tmp_path / "missing")})
        elif reason == "auth":
            runtime.codex.authenticated = False
        else:
            store.update_tasklet(task["id"], {"prompt": "/goal\nresume"})
    elif reason == "command":
        store.update_tasklet(task["id"], {"prompt": "/unknown"})
    elif reason == "active":
        await runtime.start()
    before = database(store)
    with pytest.raises(HTTPException) as error:
        await runtime.start(selected)
    assert error.value.status_code in {404, 409, 422}
    assert database(store) == before
    if reason == "active":
        await runtime.stop()


async def test_acceptance_failure_rolls_back_chat_sessions_statuses_and_run(environment):
    store, runtime, provider = environment
    a, b = [old_task(store, name) for name in ("A", "B")]
    store.create_edge({"source": a["id"], "target": b["id"]})
    store.connection.execute(
        "CREATE TRIGGER fail_run BEFORE INSERT ON runs "
        "BEGIN SELECT RAISE(ABORT,'injected run failure'); END"
    )
    before = database(store)
    queue = runtime.events.subscribe()
    with pytest.raises(sqlite3.IntegrityError, match="injected run failure"):
        await runtime.start([a["id"]])
    assert database(store) == before
    assert queue.empty()
    assert runtime.worker is None
    assert provider.calls == []


async def test_provider_failure_after_acceptance_keeps_only_new_attempt(environment):
    store, runtime, provider = environment
    task = old_task(store, "A")
    provider.failures.add("A")
    pipeline = await runtime.start()
    await finished(runtime)
    assert store.tasklet(task["id"])["status"] == "failed"
    assert all(message["conversation_id"] == pipeline["id"] for message in store.messages(task["id"]))
    assert "old" not in str(store.messages(task["id"]))
    assert store.codex_session(task["id"]) is None


@pytest.mark.parametrize("command", ["/goal", "/goal inspect", "/goal pause", "/goal clear", "/stop", "/goal resume", "/goal new objective", "ordinary follow-up"])
async def test_chat_commands_keep_existing_conversation_and_session(environment, command):
    store, runtime, _ = environment
    store.save_settings({"execution_mode": "codex"})
    task = old_task(store, "A")
    before = conversation(store, task)
    await runtime.send_message(task["id"], command)
    if runtime.worker:
        await finished(runtime)
    messages, session, _, generation = conversation(store, task)
    assert messages[:2] == before[0]
    assert session == before[1]
    assert generation == before[3]
    if runtime.codex.calls:
        assert {message["content"] for message in runtime.codex.calls[-1][2]} >= {"old question A", "old answer A", command}


@pytest.mark.parametrize("goal", [False, True])
async def test_fake_codex_fresh_thread_then_followup_resumes_new_thread(environment, tmp_path, monkeypatch, goal):
    store, runtime, _ = environment
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AISPACE_CODEX_COMMAND_JSON", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(audit))
    monkeypatch.setenv("FAKE_CODEX_MODE", "goal" if goal else "complete")
    store.save_settings({"execution_mode": "codex"})
    task = old_task(store, "A")
    if goal:
        store.update_tasklet(task["id"], {"prompt": "/goal fresh objective"})
    store.save_codex_session(task["id"], "old-thread-with-paused-goal", str(tmp_path))
    runtime.codex = CodexProvider(store, runtime.directories)
    try:
        await runtime.start()
        await finished(runtime)
        first = store.codex_session(task["id"])["thread_id"]
        await runtime.start()
        await finished(runtime)
        second = store.codex_session(task["id"])["thread_id"]
        assert first != second
        await runtime.send_message(task["id"], "follow-up")
        await finished(runtime)
        requests = [json.loads(line) for line in audit.read_text().splitlines()]
        assert len([event for event in requests if event.get("method") == "thread/start"]) == 2
        resumes = [event for event in requests if event.get("method") == "thread/resume"]
        assert len(resumes) == 1
        assert resumes[0]["params"]["threadId"] == second
        turns = [event for event in requests if event["kind"] == "turn" and not event["auto"]]
        prompt = "fresh objective" if goal else "A"
        assert [event["prompt"] for event in turns] == [prompt, prompt, "follow-up"]
        if goal:
            objectives = [event["params"]["objective"] for event in requests if event.get("method") == "thread/goal/set" and "objective" in event["params"]]
            assert objectives == ["fresh objective", "fresh objective"]
        assert "old-thread-with-paused-goal" not in audit.read_text()
        assert len(store.messages(task["id"])) == 4
        assert store.codex_session(task["id"])["thread_id"] == second
    finally:
        await runtime.codex.close()


def test_version_one_upgrade_preserves_existing_conversation(tmp_path):
    store = Store(tmp_path)
    task = old_task(store, "A")
    before = conversation(store, task)
    store.connection.execute("ALTER TABLE tasklets DROP COLUMN conversation_id")
    store.connection.execute("PRAGMA user_version=1")
    store.close()
    store = Store(tmp_path)
    assert conversation(store, task) == before
    assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    store.close()
