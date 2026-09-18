import asyncio
import stat
from contextlib import asynccontextmanager

import httpx

from aispace.main import create_app
from aispace.provider import ChatProvider, ProviderError
from aispace.storage import Store


class FakeProvider:
    def __init__(self, delay=0.01, failures=None):
        self.delay = delay
        self.failures = failures or set()
        self.calls = []
        self.active = 0
        self.maximum = 0
        self.finished = []
        self.cancelled = []

    async def test(self, settings):
        return {"ok": True, "message": "ok"}

    async def stream(self, settings, model, messages):
        label = messages[-1]["content"]
        self.calls.append(
            {
                "label": label,
                "model": model,
                "messages": messages,
                "finished_before": list(self.finished),
            }
        )
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        completed = False
        try:
            yield f"Начало {label}. "
            await asyncio.sleep(self.delay)
            if label in self.failures:
                raise ProviderError("Тестовая ошибка провайдера")
            yield f"Результат {label}."
            self.finished.append(label)
            completed = True
        finally:
            self.active -= 1
            if not completed:
                self.cancelled.append(label)
            await asyncio.sleep(0.005)


@asynccontextmanager
async def client_for(tmp_path, provider=None):
    app = create_app(tmp_path, provider or FakeProvider())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client, app.state.runtime


async def configure(client, **changes):
    response = await client.patch(
        "/api/settings", json={"api_key": "private-test-key", "model": "test-model", **changes}
    )
    assert response.status_code == 200


async def tasklet(client, title):
    response = await client.post("/api/tasklets", json={"title": title, "prompt": title})
    assert response.status_code == 201
    return response.json()


async def edge(client, source, target, pass_context=False):
    response = await client.post(
        "/api/edges",
        json={"source": source["id"], "target": target["id"], "pass_context": pass_context},
    )
    assert response.status_code == 201
    return response.json()


async def finished(runtime):
    await asyncio.wait_for(asyncio.shield(runtime.worker), timeout=3)


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), 2)


async def test_crud_settings_and_restart_persistence(tmp_path):
    async with client_for(tmp_path) as (client, runtime):
        assert (await client.get("/api/workspace")).json()["tasklets"] == []
        assert (await client.get("/api/settings")).json()["model"] == ""
        await configure(client, workspace_context="Общий контекст")
        assert "private-test-key" not in (await client.get("/api/settings")).text
        assert "api_key" not in (await client.get("/api/settings")).json()
        await client.patch("/api/settings", json={"max_parallel": 2})
        assert runtime.store.settings(private=True)["api_key"] == "private-test-key"
        bad = await client.patch("/api/settings", json={"api_key": "secret-" * 1000})
        assert bad.status_code == 422
        assert "secret-" not in bad.text
        first = await tasklet(client, "Первый")
        second = await tasklet(client, "Второй")
        link = await edge(client, first, second)
        response = await client.patch(
            f"/api/tasklets/{first['id']}",
            json={"title": "Изменён", "position": {"x": 10, "y": 20}},
        )
        assert response.json()["position"] == {"x": 10, "y": 20}
        response = await client.patch(f"/api/edges/{link['id']}", json={"pass_context": True})
        assert response.json()["pass_context"] is True
        assert (await client.post("/api/pipeline/start", json={})).status_code == 202
        await finished(runtime)
    assert stat.S_IMODE((tmp_path / "aispace.sqlite3").stat().st_mode) == 0o600
    async with client_for(tmp_path) as (client, runtime):
        workspace = (await client.get("/api/workspace")).json()
        assert workspace["pipeline"]["status"] == "completed"
        assert len(workspace["tasklets"]) == 2
        assert (await client.get(f"/api/tasklets/{first['id']}/messages")).json()[0][
            "content"
        ] == "Первый"
        assert (await client.get("/api/settings")).json()["api_key_configured"] is True
        assert (await client.delete(f"/api/tasklets/{first['id']}")).status_code == 204
        assert (await client.get("/api/workspace")).json()["edges"] == []
        assert (await client.get(f"/api/tasklets/{first['id']}/messages")).status_code == 404
        assert (await client.patch("/api/settings", json={"api_key": None})).json()[
            "api_key_configured"
        ] is False


async def test_edges_reject_cycles_duplicates_and_missing_nodes(tmp_path):
    async with client_for(tmp_path) as (client, _):
        first, second, third = [await tasklet(client, name) for name in ["a", "b", "c"]]
        await edge(client, first, second)
        link = await edge(client, second, third)
        for source, target, status in [
            (third["id"], first["id"], 422),
            (first["id"], first["id"], 422),
            (first["id"], second["id"], 409),
            (first["id"], "missing", 404),
        ]:
            response = await client.post("/api/edges", json={"source": source, "target": target})
            assert response.status_code == status
        assert (await client.delete(f"/api/edges/{link['id']}")).status_code == 204
        assert len((await client.get("/api/workspace")).json()["edges"]) == 1


async def test_parallel_dag_and_optional_context(tmp_path):
    provider = FakeProvider(delay=0.03)
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client, max_parallel=2, workspace_context="Инструкция пространства")
        first, second, dependent = [await tasklet(client, name) for name in ["a", "b", "c"]]
        await edge(client, first, dependent, pass_context=True)
        await edge(client, second, dependent)
        response = await client.post("/api/pipeline/start", json={})
        assert response.status_code == 202
        await finished(runtime)
        assert provider.maximum == 2
        dependent_call = next(call for call in provider.calls if call["label"] == "c")
        assert set(dependent_call["finished_before"]) == {"a", "b"}
        instructions = "\n".join(message["content"] for message in dependent_call["messages"])
        assert "Результат a." in instructions
        assert "Результат b." not in instructions
        assert "Инструкция пространства" in instructions
        assert runtime.store.pipeline()["completed"] == 3
        assert all(task["status"] == "completed" for task in runtime.store.tasklets())


async def test_failed_dependency_blocks_descendants_but_not_independent(tmp_path):
    provider = FakeProvider(failures={"a"})
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client)
        first, second, third, _independent = [
            await tasklet(client, name) for name in ["a", "b", "c", "d"]
        ]
        await edge(client, first, second)
        await edge(client, second, third)
        await client.post("/api/pipeline/start", json={})
        await finished(runtime)
        statuses = {task["title"]: task["status"] for task in runtime.store.tasklets()}
        assert statuses == {"a": "failed", "b": "blocked", "c": "blocked", "d": "completed"}
        assert runtime.store.pipeline()["status"] == "failed"
        assert {call["label"] for call in provider.calls} == {"a", "d"}
        assert runtime.store.tasklet(first["id"])["last_output"].startswith("Начало")


async def test_full_stop_is_idempotent_cancels_all_and_prevents_delayed_updates(tmp_path):
    provider = FakeProvider(delay=0.3)
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client, max_parallel=2)
        first, second, third = [await tasklet(client, name) for name in ["a", "b", "c"]]
        await client.post("/api/pipeline/start", json={})
        await until(lambda: provider.active == 2)
        assert (
            await client.patch(f"/api/tasklets/{first['id']}", json={"prompt": "changed"})
        ).status_code == 409
        assert (
            await client.patch(f"/api/tasklets/{first['id']}", json={"position": {"x": 1, "y": 2}})
        ).status_code == 200
        assert (await client.patch("/api/settings", json={"model": "another"})).status_code == 409
        assert (await client.post("/api/pipeline/start", json={})).status_code == 409
        assert (await client.delete(f"/api/tasklets/{second['id']}")).status_code == 409
        responses = await asyncio.gather(
            client.post("/api/pipeline/stop"), client.post("/api/pipeline/stop")
        )
        assert all(response.json()["status"] == "cancelled" for response in responses)
        assert provider.active == 0
        assert len(provider.calls) == 2
        snapshot = runtime.store.workspace()
        assert all(task["status"] == "cancelled" for task in snapshot["tasklets"])
        await asyncio.sleep(0.35)
        assert runtime.store.workspace() == snapshot
        assert (await client.post("/api/pipeline/stop")).json() == snapshot["pipeline"]
        assert (await client.patch("/api/settings", json={"max_parallel": 1})).status_code == 200
        provider.delay = 0.001
        await client.post("/api/pipeline/start", json={"tasklet_ids": [third["id"]]})
        await finished(runtime)
        assert runtime.store.pipeline()["status"] == "completed"


async def test_selected_task_includes_unfinished_ancestors_and_prompt_edits_invalidate(tmp_path):
    provider = FakeProvider()
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client)
        first, second, unrelated = [await tasklet(client, name) for name in ["a", "b", "other"]]
        await edge(client, first, second)
        response = await client.post("/api/pipeline/start", json={"tasklet_ids": [second["id"]]})
        assert response.json()["total"] == 2
        await finished(runtime)
        assert [call["label"] for call in provider.calls] == ["a", "b"]
        await client.patch(f"/api/tasklets/{first['id']}", json={"prompt": "a-updated"})
        assert runtime.store.tasklet(first["id"])["status"] == "idle"
        assert runtime.store.tasklet(second["id"])["status"] == "idle"
        response = await client.post("/api/pipeline/start", json={"tasklet_ids": [second["id"]]})
        assert response.json()["total"] == 2
        await finished(runtime)
        assert [call["label"] for call in provider.calls] == ["a", "b", "a-updated", "b"]
        await client.post("/api/pipeline/start", json={"tasklet_ids": [first["id"]]})
        await finished(runtime)
        assert runtime.store.tasklet(second["id"])["status"] == "idle"
        assert runtime.store.tasklet(unrelated["id"])["status"] == "idle"


async def test_chat_continues_persisted_history_and_model_override(tmp_path):
    provider = FakeProvider()
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client)
        task = await tasklet(client, "Первый вопрос")
        await client.patch(f"/api/tasklets/{task['id']}", json={"model": "special-model"})
        await client.post("/api/pipeline/start", json={})
        await finished(runtime)
        response = await client.post(
            f"/api/tasklets/{task['id']}/messages", json={"content": "Уточнение"}
        )
        assert response.status_code == 202
        await finished(runtime)
        messages = (await client.get(f"/api/tasklets/{task['id']}/messages")).json()
        assert [message["role"] for message in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert provider.calls[-1]["model"] == "special-model"
        assert provider.calls[-1]["messages"][0]["content"] == "Первый вопрос"
        assert provider.calls[-1]["messages"][1]["role"] == "assistant"
        assert provider.calls[-1]["messages"][-1]["content"] == "Уточнение"


async def test_run_requires_valid_settings_and_nonempty_prompt(tmp_path):
    async with client_for(tmp_path) as (client, _):
        assert (await client.post("/api/pipeline/start", json={})).status_code == 422
        task = await tasklet(client, "a")
        assert (await client.post("/api/pipeline/start", json={})).status_code == 422
        await client.patch("/api/settings", json={"api_key": "key"})
        assert (await client.post("/api/pipeline/start", json={})).status_code == 422
        await configure(client)
        await client.patch(f"/api/tasklets/{task['id']}", json={"prompt": ""})
        assert (await client.post("/api/pipeline/start", json={})).status_code == 422
        assert (
            await client.post("/api/pipeline/start", json={"tasklet_ids": ["missing"]})
        ).status_code == 404
        assert (
            await client.patch("/api/settings", json={"base_url": "https://user:password@host/v1"})
        ).status_code == 422


def test_startup_recovers_interrupted_pipeline(tmp_path):
    store = Store(tmp_path)
    task = store.create_tasklet(
        {"title": "a", "prompt": "a", "model": None, "position": {"x": 0, "y": 0}}
    )
    store.update_tasklet(task["id"], {"status": "running"})
    store.save_pipeline(
        {
            "id": "interrupted",
            "status": "running",
            "started_at": "then",
            "finished_at": None,
            "total": 1,
            "completed": 0,
            "error": None,
        }
    )
    store.close()
    store = Store(tmp_path)
    assert store.pipeline()["status"] == "cancelled"
    assert store.tasklet(task["id"])["status"] == "cancelled"
    assert "перезапуском" in store.pipeline()["error"]
    store.close()


async def test_browser_origin_guard(tmp_path):
    async with client_for(tmp_path) as (client, _):
        response = await client.patch(
            "/api/settings",
            json={"api_key": "changed"},
            headers={"Origin": "https://untrusted.example"},
        )
        assert response.status_code == 403
        assert (await client.get("/api/settings")).json()["api_key_configured"] is False
        response = await client.patch(
            "/api/settings",
            json={"model": "model"},
            headers={"Origin": "http://localhost:8080"},
        )
        assert response.status_code == 200


async def test_empty_stream_cannot_unlock_dependency(tmp_path):
    provider = ChatProvider(
        httpx.MockTransport(lambda request: httpx.Response(200, text="data: [DONE]\n\n"))
    )
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client)
        first = await tasklet(client, "a")
        second = await tasklet(client, "b")
        await edge(client, first, second)
        await client.post("/api/pipeline/start", json={})
        await finished(runtime)
        assert runtime.store.tasklet(first["id"])["status"] == "failed"
        assert runtime.store.tasklet(second["id"])["status"] == "blocked"
        assert "без текстового ответа" in runtime.store.tasklet(first["id"])["error"]


async def test_single_worker_limit_is_respected(tmp_path):
    provider = FakeProvider(delay=0.01)
    async with client_for(tmp_path, provider) as (client, runtime):
        await configure(client, max_parallel=1)
        for name in ("a", "b", "c"):
            await tasklet(client, name)
        await client.post("/api/pipeline/start", json={})
        await finished(runtime)
        assert len(provider.calls) == 3
        assert provider.maximum == 1
        assert runtime.store.pipeline()["status"] == "completed"
