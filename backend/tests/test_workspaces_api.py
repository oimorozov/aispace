import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from aispace.main import create_app


class WorkspaceProvider:
    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.active = 0
        self.maximum = 0

    async def stream(self, settings, model, messages):
        self.calls.append({"settings": dict(settings), "model": model, "messages": messages})
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.set()
        try:
            yield "Начало. "
            await self.release.wait()
            yield "Готово."
        finally:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.active -= 1


class WorkspaceCodex(WorkspaceProvider):
    def __init__(self):
        super().__init__()
        self.commands = []

    async def status(self):
        return {"authenticated": True, "message": "Подключено"}

    async def stream(self, settings, model, messages, tasklet):
        async for value in super().stream(settings, model, messages):
            yield value

    async def command(self, tasklet, settings, content):
        self.commands.append((tasklet["id"], content))
        return "Цель на паузе"

    async def close(self):
        pass


@asynccontextmanager
async def client_for(tmp_path, provider=None, codex=None):
    provider = provider or WorkspaceProvider()
    app = create_app(tmp_path / "data", provider, codex or WorkspaceCodex(), tmp_path)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.patch(
            "/api/settings", json={"api_key": "fixture-key", "model": "fixture-model"}
        )
        assert response.status_code == 200
        yield client, app.state.runtime, provider, app


async def workspace(client, name):
    response = await client.post("/api/workspaces", json={"name": name})
    assert response.status_code == 201
    return response.json()


async def tasklet(client, owner, title):
    response = await client.post(
        f"/api/workspaces/{owner['id']}/tasklets", json={"title": title, "prompt": title}
    )
    assert response.status_code == 201
    assert response.json()["workspace_id"] == owner["id"]
    return response.json()


async def finish(runtime):
    await asyncio.wait_for(asyncio.shield(runtime.worker), 2)


async def test_workspace_crud_keeps_global_settings_separate_and_allows_zero_workspaces(tmp_path):
    async with client_for(tmp_path) as (client, runtime, _, _):
        first = await workspace(client, "Первый проект")
        second = await workspace(client, "Второй проект")
        assert first["tasklets"] == first["edges"] == []
        assert first["workspace_context"] == ""
        assert first["working_directory"] is None
        assert first["pipeline"]["status"] == "idle"
        project = tmp_path / "project"
        project.mkdir()
        marker = project / "keep.txt"
        marker.write_text("unchanged")
        before_settings = runtime.store.settings(private=True)
        changed = await client.patch(
            f"/api/workspaces/{first['id']}",
            json={
                "name": "Переименован",
                "workspace_context": "Контекст A",
                "working_directory": str(project),
            },
        )
        assert changed.status_code == 200
        assert changed.json()["id"] == first["id"]
        assert changed.json()["name"] == "Переименован"
        assert (await client.get(f"/api/workspaces/{second['id']}")).json() == second
        settings = (await client.get("/api/settings")).json()
        assert "working_directory" not in settings
        assert "workspace_context" not in settings
        assert (
            await client.patch("/api/settings", json={"workspace_context": "wrong"})
        ).status_code == 422
        assert (
            await client.patch(f"/api/workspaces/{first['id']}", json={"model": "wrong"})
        ).status_code == 422
        assert (await client.post("/api/workspaces", json={"name": "   "})).status_code == 422
        for summary in (await client.get("/api/workspaces")).json():
            assert "tasklets" not in summary
            assert "workspace_context" not in summary
            assert (await client.delete(f"/api/workspaces/{summary['id']}")).status_code == 204
        assert (await client.get("/api/workspaces")).json() == []
        assert runtime.store.default_workspace_id is None
        assert runtime.store.settings(private=True) == before_settings
        assert marker.read_text() == "unchanged"
        assert (await client.get(f"/api/workspaces/{first['id']}")).status_code == 404


async def test_foreign_tasklets_and_edges_cannot_be_read_changed_or_executed(tmp_path):
    async with client_for(tmp_path) as (client, runtime, provider, _):
        first = await workspace(client, "A")
        second = await workspace(client, "B")
        a = await tasklet(client, first, "A1")
        a2 = await tasklet(client, first, "A2")
        b = await tasklet(client, second, "B1")
        edge = await client.post(
            f"/api/workspaces/{first['id']}/edges", json={"source": a["id"], "target": a2["id"]}
        )
        assert edge.status_code == 201
        prefix = f"/api/workspaces/{second['id']}"
        before = [runtime.store.workspace(item["id"]) for item in (first, second)]
        attempts = [
            ("GET", f"{prefix}/tasklets/{a['id']}/messages", None),
            ("PATCH", f"{prefix}/tasklets/{a['id']}", {"prompt": "wrong"}),
            ("DELETE", f"{prefix}/tasklets/{a['id']}", None),
            ("POST", f"{prefix}/tasklets/{a['id']}/messages", {"content": "wrong"}),
            ("POST", f"{prefix}/pipeline/start", {"tasklet_ids": [a["id"]]}),
            ("POST", f"{prefix}/edges", {"source": a["id"], "target": b["id"]}),
            ("POST", f"{prefix}/edges", {"source": a["id"], "target": a2["id"]}),
            ("PATCH", f"{prefix}/edges/{edge.json()['id']}", {"pass_context": True}),
            ("DELETE", f"{prefix}/edges/{edge.json()['id']}", None),
        ]
        for method, path, payload in attempts:
            response = await client.request(method, path, json=payload)
            assert response.status_code == 404
        assert [runtime.store.workspace(item["id"]) for item in (first, second)] == before
        assert provider.calls == []
        assert (await client.get("/api/workspace")).status_code == 404
        assert (await client.post("/api/tasklets", json={"title": "unscoped"})).status_code == 404
        assert (await client.post("/api/pipeline/stop")).status_code == 404
        assert (await client.post("/api/workspaces/missing/pipeline/stop")).status_code == 404


async def test_idle_workspace_stays_editable_and_stop_delete_are_scoped_during_cleanup(tmp_path):
    async with client_for(tmp_path) as (client, runtime, provider, _):
        first = await workspace(client, "Работающий A")
        second = await workspace(client, "Свободный B")
        await tasklet(client, first, "Долгая A")
        b = await tasklet(client, second, "Задача B")
        a_url = f"/api/workspaces/{first['id']}"
        b_url = f"/api/workspaces/{second['id']}"
        assert (await client.post(f"{a_url}/pipeline/start")).status_code == 202
        await asyncio.wait_for(provider.started.wait(), 1)
        conflict = await client.post(f"{b_url}/pipeline/start")
        assert conflict.status_code == 409
        assert first["name"] in conflict.json()["detail"]
        assert (await client.patch(a_url, json={"workspace_context": "new A"})).status_code == 409
        assert (await client.patch(b_url, json={"workspace_context": "new B"})).status_code == 200
        assert (
            await client.patch(f"{b_url}/tasklets/{b['id']}", json={"prompt": "edited"})
        ).status_code == 200
        assert (await client.patch("/api/settings", json={"model": "other"})).status_code == 409
        assert (await client.delete(a_url)).status_code == 409
        assert (await client.post(f"{b_url}/pipeline/stop")).json()["status"] == "idle"
        assert runtime.active_workspace_id == first["id"]
        assert provider.active == 1
        assert (await client.delete(b_url)).status_code == 204
        assert provider.active == 1
        provider.cleanup_release.clear()
        stopped = asyncio.create_task(client.post(f"{a_url}/pipeline/stop"))
        await asyncio.wait_for(provider.cleanup_started.wait(), 1)
        assert (await client.get(a_url)).json()["pipeline"]["status"] == "stopping"
        assert (await client.delete(a_url)).status_code == 409
        assert (await client.patch("/api/settings", json={"model": "other"})).status_code == 409
        provider.cleanup_release.set()
        assert (await stopped).json()["status"] == "cancelled"
        assert provider.active == 0
        assert (await client.delete(a_url)).status_code == 204


async def test_concurrent_cross_workspace_starts_allow_exactly_one_worker(tmp_path):
    async with client_for(tmp_path) as (client, runtime, provider, _):
        owners = [await workspace(client, name) for name in ("A", "B")]
        for owner in owners:
            await tasklet(client, owner, owner["name"])
        responses = await asyncio.gather(
            *(client.post(f"/api/workspaces/{owner['id']}/pipeline/start") for owner in owners)
        )
        assert sorted(response.status_code for response in responses) == [202, 409]
        await asyncio.wait_for(provider.started.wait(), 1)
        assert len(provider.calls) == 1
        assert provider.maximum == 1
        active = runtime.active_workspace_id
        loser = next(owner for owner in owners if owner["id"] != active)
        assert runtime.store.pipeline(loser["id"])["status"] == "idle"
        await client.post(f"/api/workspaces/{active}/pipeline/stop")
        provider.release.set()
        assert (
            await client.post(f"/api/workspaces/{loser['id']}/pipeline/start")
        ).status_code == 202
        await finish(runtime)
        assert runtime.store.pipeline(active)["status"] == "cancelled"
        assert runtime.store.pipeline(loser["id"])["status"] == "completed"


async def test_late_stop_response_cannot_overwrite_a_new_run_of_the_same_workspace(tmp_path):
    async with client_for(tmp_path) as (client, runtime, provider, _):
        owner = await workspace(client, "Повторный запуск")
        await tasklet(client, owner, "Задача")
        first = await runtime.start(workspace_id=owner["id"])
        first_worker = runtime.worker
        await asyncio.wait_for(provider.started.wait(), 1)
        provider.cleanup_release.clear()
        first_stop = asyncio.create_task(runtime.stop(owner["id"]))
        await asyncio.wait_for(provider.cleanup_started.wait(), 1)
        async with runtime.lock:
            restarted = asyncio.create_task(runtime.start(workspace_id=owner["id"]))
            second_stop = asyncio.create_task(runtime.stop(owner["id"]))
            await asyncio.sleep(0)
            provider.cleanup_release.set()
            await first_worker
            await asyncio.sleep(0)
        second = await restarted
        stopped_first, stopped_second = await asyncio.gather(first_stop, second_stop)
        assert first["id"] != second["id"]
        assert stopped_first["id"] == first["id"]
        assert stopped_first["status"] == "cancelled"
        assert stopped_second["id"] == second["id"]
        assert stopped_second["status"] == "cancelled"
        assert runtime.store.pipeline(owner["id"])["id"] == second["id"]
        assert runtime.store.pipeline(owner["id"])["status"] == "cancelled"
        assert [run["id"] for run in runtime.store.runs(owner["id"])] == [first["id"], second["id"]]


async def test_context_chat_and_run_history_belong_to_their_workspace(tmp_path):
    async with client_for(tmp_path) as (client, runtime, provider, _):
        provider.release.set()
        owners = [await workspace(client, name) for name in ("A", "B")]
        tasks = []
        for owner in owners:
            directory = tmp_path / owner["name"]
            directory.mkdir()
            assert (
                await client.patch(
                    f"/api/workspaces/{owner['id']}",
                    json={
                        "workspace_context": f"Context {owner['name']}",
                        "working_directory": str(directory),
                    },
                )
            ).status_code == 200
            tasks.append(await tasklet(client, owner, f"Task {owner['name']}"))
            assert (
                await client.post(f"/api/workspaces/{owner['id']}/pipeline/start")
            ).status_code == 202
            await finish(runtime)
        for index, owner in enumerate(owners):
            call = provider.calls[index]
            assert call["settings"]["working_directory"] == str(tmp_path / owner["name"])
            assert call["messages"][0] == {"role": "system", "content": f"Context {owner['name']}"}
            messages = (
                await client.get(
                    f"/api/workspaces/{owner['id']}/tasklets/{tasks[index]['id']}/messages"
                )
            ).json()
            assert len(messages) == 2
            assert {message["workspace_id"] for message in messages} == {owner["id"]}
            assert {message["run_id"] for message in messages} == {
                runtime.store.pipeline(owner["id"])["id"]
            }
            assert len(runtime.store.runs(owner["id"])) == 1
        first_snapshot = runtime.store.workspace(owners[0]["id"])
        assert (
            await client.patch(
                f"/api/workspaces/{owners[1]['id']}", json={"workspace_context": "Changed B"}
            )
        ).status_code == 200
        assert runtime.store.workspace(owners[0]["id"]) == first_snapshot
        assert runtime.store.tasklets(owners[1]["id"])[0]["status"] == "idle"


async def test_slash_stop_and_pause_in_another_workspace_never_stop_active_codex(tmp_path):
    codex = WorkspaceCodex()
    async with client_for(tmp_path, codex=codex) as (client, runtime, _, _):
        owners = [await workspace(client, name) for name in ("A", "B")]
        tasks = []
        for owner in owners:
            assert (
                await client.patch(
                    f"/api/workspaces/{owner['id']}", json={"working_directory": str(tmp_path)}
                )
            ).status_code == 200
            tasks.append(await tasklet(client, owner, owner["name"]))
        assert (
            await client.patch("/api/settings", json={"execution_mode": "codex", "api_key": None})
        ).status_code == 200
        assert (
            await client.post(f"/api/workspaces/{owners[0]['id']}/pipeline/start")
        ).status_code == 202
        await asyncio.wait_for(codex.started.wait(), 1)
        second_chat = f"/api/workspaces/{owners[1]['id']}/tasklets/{tasks[1]['id']}/messages"
        response = await client.post(second_chat, json={"content": "/stop"})
        assert response.status_code == 202
        assert response.json()["status"] == "idle"
        assert (await client.post(second_chat, json={"content": "/goal pause"})).status_code == 409
        assert runtime.active_workspace_id == owners[0]["id"]
        assert codex.active == 1
        foreign_chat = f"/api/workspaces/{owners[1]['id']}/tasklets/{tasks[0]['id']}/messages"
        assert (await client.post(foreign_chat, json={"content": "/stop"})).status_code == 404
        assert codex.active == 1
        await client.post(f"/api/workspaces/{owners[0]['id']}/pipeline/stop")
        assert codex.active == 0


@pytest.mark.parametrize("delete_owner", [False, True])
async def test_queued_slash_command_rechecks_ownership_after_acquiring_lock(tmp_path, delete_owner):
    codex = WorkspaceCodex()
    async with client_for(tmp_path, codex=codex) as (client, runtime, _, _):
        owner = await workspace(client, "Удаляемое пространство")
        task = await tasklet(client, owner, "Удаляемый тасклет")
        assert (
            await client.patch("/api/settings", json={"execution_mode": "codex"})
        ).status_code == 200
        async with runtime.lock:
            pending = asyncio.create_task(
                runtime.send_message(task["id"], "/goal inspect", owner["id"])
            )
            await asyncio.sleep(0)
            if delete_owner:
                runtime.store.delete_workspace(owner["id"])
            else:
                runtime.store.delete_tasklet(task["id"], workspace_id=owner["id"])
        with pytest.raises(HTTPException) as error:
            await pending
        assert error.value.status_code == 404
        assert codex.commands == []


async def test_sse_initial_list_and_updates_carry_workspace_ownership(tmp_path):
    async with client_for(tmp_path) as (client, runtime, provider, app):
        provider.release.set()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        endpoint = next(
            route.endpoint for route in app.routes if getattr(route, "path", None) == "/api/events"
        )
        response = await endpoint(
            Request(
                {"type": "http", "method": "GET", "path": "/api/events", "headers": []}, receive
            )
        )
        iterator = response.body_iterator

        async def event():
            raw = await asyncio.wait_for(anext(iterator), 1)
            kind, data = raw.strip().split("\n", 1)
            return kind.removeprefix("event: "), json.loads(data.removeprefix("data: "))

        try:
            kind, payload = await event()
            assert kind == "workspaces"
            assert payload == runtime.store.workspaces()
            owner = await workspace(client, "Event A")
            task = await tasklet(client, owner, "Event task")
            assert (
                await client.post(f"/api/workspaces/{owner['id']}/pipeline/start")
            ).status_code == 202
            await finish(runtime)
            captured = []
            queue = next(iter(runtime.events.subscribers))
            while not queue.empty():
                captured.append(await event())
            snapshots = [value for kind, value in captured if kind == "workspace"]
            assert snapshots and all(value["id"] == owner["id"] for value in snapshots)
            messages = [value for kind, value in captured if kind == "message"]
            assert messages and all(
                value["workspace_id"] == owner["id"] and value["tasklet_id"] == task["id"]
                for value in messages
            )
            lists = [value for kind, value in captured if kind == "workspaces"]
            assert any(
                item["pipeline"]["status"] == "running"
                for value in lists
                for item in value
                if item["id"] == owner["id"]
            )
            assert lists[-1] == runtime.store.workspaces()
        finally:
            await iterator.aclose()
        assert not runtime.events.subscribers
