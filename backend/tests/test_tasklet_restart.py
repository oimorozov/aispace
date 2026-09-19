import asyncio
import json
import sys

import pytest
import test_fresh_runs
from fastapi import HTTPException
from test_api import FakeProvider, client_for, configure, finished, tasklet, until
from test_codex import FAKE
from test_fresh_runs import conversation, database, old_task

from aispace.codex import CodexConnection, CodexProvider
from aispace.models import Pipeline

environment = test_fresh_runs.environment


async def test_restart_executes_exact_target_and_preserves_other_conversations(environment):
    store, runtime, provider = environment
    a, b, c, d = [old_task(store, label) for label in ("A", "B", "C", "D")]
    store.create_edge({"source": a["id"], "target": b["id"], "pass_context": True})
    store.create_edge({"source": b["id"], "target": c["id"]})
    store.save_pipeline(Pipeline(id="old-run", status="completed").model_dump())
    before = {task["id"]: conversation(store, task) for task in (a, c, d)}
    pipeline = await runtime.restart(b["id"])
    assert pipeline["total"] == 1
    assert store.tasklet(b["id"])["status"] == "queued"
    assert conversation(store, b) == ([], None, "", pipeline["id"])
    assert store.tasklet(c["id"])["status"] == "idle"
    assert store.tasklet(a["id"])["status"] == store.tasklet(d["id"])["status"] == "completed"
    await finished(runtime)
    assert [call["label"] for call in provider.calls] == ["B"]
    assert provider.calls[0]["messages"] == [
        {"role": "system", "content": "Результат задачи «A»:\nold result A"},
        {"role": "user", "content": "B"},
    ]
    assert {task["id"]: conversation(store, task) for task in (a, c, d)} == before
    assert [run["id"] for run in store.runs()] == ["old-run", pipeline["id"]]
    await runtime.send_message(b["id"], "continue")
    await finished(runtime)
    assert len(store.messages(b["id"])) == 4
    assert store.tasklet(b["id"])["conversation_id"] == pipeline["id"]


@pytest.mark.parametrize("status", ["idle", "queued", "running", "failed", "cancelled", "blocked"])
@pytest.mark.parametrize("transitive", [False, True])
async def test_restart_rejects_every_incomplete_ancestor_without_changes(environment, status, transitive):
    store, runtime, provider = environment
    a, b, c = [old_task(store, label) for label in ("A", "B", "C")]
    store.create_edge({"source": a["id"], "target": b["id"]})
    store.create_edge({"source": b["id"], "target": c["id"]})
    store.update_tasklet(a["id"] if transitive else b["id"], {"status": status})
    before = database(store)
    with pytest.raises(HTTPException) as error:
        await runtime.restart(c["id"])
    assert error.value.status_code == 409
    assert "зависимости" in error.value.detail
    assert database(store) == before
    assert provider.calls == []
    assert runtime.worker is None


async def test_restart_endpoint_scopes_owner_and_accepts_only_one_concurrent_request(tmp_path):
    provider = FakeProvider(delay=0.1)
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client)
        task = await tasklet(client, "one")
        path = f"{client.workspace_path}/tasklets/{task['id']}/restart"
        first, second = await asyncio.gather(client.post(path), client.post(path))
        assert sorted([first.status_code, second.status_code]) == [202, 409]
        await finished(runtime)
        assert len(provider.calls) == 1
        other = runtime.store.create_workspace({"name": "Other"})["id"]
        before = database(runtime.store)
        response = await client.post(f"/api/workspaces/{other}/tasklets/{task['id']}/restart")
        assert response.status_code == 404
        assert database(runtime.store) == before
        assert (await client.post(f"{client.workspace_path}/tasklets/missing/restart")).status_code == 404


@pytest.mark.parametrize("reason", ["prompt", "key", "command", "directory", "auth", "resume"])
async def test_restart_validation_preserves_chat_results_and_sessions(environment, tmp_path, reason):
    store, runtime, provider = environment
    task = old_task(store, "A")
    if reason == "prompt":
        store.update_tasklet(task["id"], {"prompt": ""})
    elif reason == "key":
        store.save_settings({"api_key": None})
    elif reason == "command":
        store.update_tasklet(task["id"], {"prompt": "/unsupported"})
    else:
        store.save_settings({"execution_mode": "codex"})
        if reason == "directory":
            store.update_tasklet(task["id"], {"working_directory": str(tmp_path / "missing")})
        elif reason == "auth":
            runtime.codex.authenticated = False
        else:
            store.update_tasklet(task["id"], {"prompt": "/goal resume"})
    before = database(store)
    with pytest.raises(HTTPException) as error:
        await runtime.restart(task["id"])
    assert error.value.status_code == 422
    assert database(store) == before
    assert provider.calls == []


async def test_restart_waits_for_codex_process_cleanup_and_uses_new_thread(environment, tmp_path, monkeypatch):
    store, runtime, _ = environment
    script = tmp_path / "fake.py"
    script.write_text(FAKE)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AISPACE_CODEX_COMMAND_JSON", json.dumps([sys.executable, str(script)]))
    monkeypatch.setenv("FAKE_CODEX_LOG", str(audit))
    monkeypatch.setenv("FAKE_CODEX_MODE", "ignore_interrupt")
    monkeypatch.setattr(CodexConnection, "stop_timeout", 0.1)
    monkeypatch.setattr(CodexConnection, "process_timeout", 0.1)
    store.save_settings({"execution_mode": "codex"})
    a, b = [old_task(store, label) for label in ("A", "B")]
    store.save_codex_session(b["id"], "old-thread-B", str(tmp_path))
    runtime.codex = CodexProvider(store, runtime.directories)
    try:
        await runtime.restart(a["id"])
        await until(lambda: audit.exists() and '"kind": "turn"' in audit.read_text())
        connection = runtime.codex.running[a["id"]][0]
        stopping = asyncio.create_task(runtime.stop())
        await until(lambda: store.pipeline()["status"] == "stopping")
        before = conversation(store, b)
        with pytest.raises(HTTPException) as error:
            await runtime.restart(b["id"])
        assert error.value.status_code == 409
        assert conversation(store, b) == before
        await stopping
        assert connection.process.returncode is not None
        assert not runtime.codex.running
        monkeypatch.setenv("FAKE_CODEX_MODE", "complete")
        await runtime.restart(b["id"])
        await finished(runtime)
        new_thread = store.codex_session(b["id"])["thread_id"]
        assert new_thread != "old-thread-B"
        requests = [json.loads(line) for line in audit.read_text().splitlines()]
        assert not any(event.get("method") == "thread/resume" for event in requests)
        before_a = conversation(store, a)
        await runtime.send_message(b["id"], "follow-up")
        await finished(runtime)
        assert store.codex_session(b["id"])["thread_id"] == new_thread
        assert conversation(store, a) == before_a
        assert len(store.messages(b["id"])) == 4
    finally:
        await runtime.codex.close()
