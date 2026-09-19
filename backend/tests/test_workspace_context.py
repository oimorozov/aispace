import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from aispace.main import create_app
from aispace.storage import Store


class StubCodex:
    def __init__(self):
        self.calls = []
        self.authenticated = True

    async def status(self):
        return {
            "available": True,
            "authenticated": self.authenticated,
            "auth_type": "chatgpt",
            "account_label": "pro",
            "message": "Подключено" if self.authenticated else "Войдите через ChatGPT",
            "capabilities": {"workspace": True, "commands": ["/goal", "/stop"]},
        }

    async def stream(self, settings, model, messages, tasklet):
        self.calls.append((settings, model, messages, tasklet))
        yield "Готово"

    async def command(self, tasklet, settings, text):
        return "Цель: тестовая цель"

    async def close(self):
        pass


@asynccontextmanager
async def context(tmp_path, codex=None):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    app = create_app(tmp_path / "data", codex_provider=codex or StubCodex(), workspace_root=project)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client, app.state.runtime, project


async def test_directory_picker_and_validation_cannot_escape_root(tmp_path):
    async with context(tmp_path) as (client, _runtime, project):
        (project / "source").mkdir()
        (project / ".private").mkdir()
        (project / "escape").symlink_to(tmp_path, target_is_directory=True)
        listing = (await client.get("/api/directories")).json()
        assert listing["parent"] is None
        assert [entry["name"] for entry in listing["entries"]] == [".private", "source"]
        for path in (tmp_path, project / "escape", project / "missing"):
            assert (
                await client.get("/api/directories", params={"path": str(path)})
            ).status_code == 422
            assert (
                await client.patch("/api/settings", json={"working_directory": str(path)})
            ).status_code == 422
        assert (
            await client.post(
                "/api/tasklets", json={"title": "test", "working_directory": str(tmp_path)}
            )
        ).status_code == 422


async def test_default_directory_picker_can_select_folders_outside_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    outside = tmp_path / "another-project"
    hidden = outside / ".configuration"
    project.mkdir()
    hidden.mkdir(parents=True)
    monkeypatch.delenv("AISPACE_WORKSPACE_ROOT", raising=False)
    monkeypatch.chdir(project)
    app = create_app(tmp_path / "data", codex_provider=StubCodex())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        root = (await client.get("/api/directories")).json()
        assert root["path"] == str(Path("/").resolve())
        assert root["parent"] is None
        listing = (await client.get("/api/directories", params={"path": str(outside)})).json()
        assert listing["parent"] == str(tmp_path.resolve())
        assert listing["entries"] == [{"name": hidden.name, "path": str(hidden.resolve())}]
        settings = await client.patch("/api/settings", json={"working_directory": str(outside)})
        assert settings.status_code == 200
        assert settings.json()["working_directory"] == str(outside.resolve())
        tasklet = await client.post(
            "/api/tasklets", json={"title": "External project", "working_directory": str(hidden)}
        )
        assert tasklet.status_code == 201
        assert tasklet.json()["working_directory"] == str(hidden)


async def test_native_picker_routes_do_not_save_or_allow_foreign_origins(tmp_path, monkeypatch):
    async with context(tmp_path) as (client, runtime, project):
        calls = []

        async def choose(value=None):
            calls.append(value)
            return {"path": str(project) if value else None}

        monkeypatch.setattr(runtime.directories, "choose", choose)
        monkeypatch.setattr(
            runtime.directories,
            "capabilities",
            lambda: {"native_picker": True, "platform": "darwin"},
        )
        assert (await client.get("/api/directories/capabilities")).json()["native_picker"]
        settings = (await client.get("/api/settings")).json()
        selected = await client.post("/api/directories/choose", json={"path": str(project)})
        assert selected.status_code == 200
        assert selected.json() == {"path": str(project)}
        assert (await client.post("/api/directories/choose")).json() == {"path": None}
        assert (await client.get("/api/settings")).json() == settings
        assert (
            await client.post("/api/directories/choose", headers={"Origin": "https://example.com"})
        ).status_code == 403
        assert calls == [str(project), None]


async def test_codex_runs_without_api_key_and_preserves_workspace_overrides(tmp_path):
    codex = StubCodex()
    async with context(tmp_path, codex) as (client, runtime, project):
        nested = project / "source"
        nested.mkdir()
        response = await client.patch(
            "/api/settings", json={"execution_mode": "codex", "working_directory": str(project)}
        )
        assert response.status_code == 200
        assert response.json()["api_key_configured"] is False
        assert response.json()["model"] == ""
        tasklet = (
            await client.post(
                "/api/tasklets",
                json={"title": "test", "prompt": "Read files", "working_directory": str(nested)},
            )
        ).json()
        assert (await client.post("/api/pipeline/start")).status_code == 202
        await asyncio.wait_for(runtime.worker, 2)
        assert runtime.store.tasklet(tasklet["id"])["status"] == "completed"
        assert codex.calls[0][3]["working_directory"] == str(nested)
        assert codex.calls[0][0]["codex_sandbox"] == "read-only"
        runtime.store.save_codex_session(tasklet["id"], "test-thread", str(nested))
    async with context(tmp_path) as (client, runtime, project):
        assert (await client.get("/api/settings")).json()["execution_mode"] == "codex"
        assert runtime.store.tasklet(tasklet["id"])["working_directory"] == str(nested)
        assert runtime.store.codex_session(tasklet["id"])["thread_id"] == "test-thread"
        await client.patch(f"/api/tasklets/{tasklet['id']}", json={"working_directory": None})
        assert runtime.store.tasklet(tasklet["id"])["working_directory"] is None


async def test_codex_checks_auth_and_directory_before_start(tmp_path):
    codex = StubCodex()
    async with context(tmp_path, codex) as (client, runtime, project):
        await client.patch("/api/settings", json={"execution_mode": "codex"})
        await client.post("/api/tasklets", json={"title": "test", "prompt": "Read files"})
        assert (await client.post("/api/pipeline/start")).status_code == 422
        await client.patch("/api/settings", json={"working_directory": str(project)})
        codex.authenticated = False
        response = await client.post("/api/pipeline/start")
        assert response.status_code == 422
        assert "Войдите" in response.json()["detail"]
        assert runtime.store.pipeline()["status"] == "idle"
        assert not codex.calls


async def test_harness_commands_are_not_silently_forwarded_to_api(tmp_path):
    async with context(tmp_path) as (client, runtime, project):
        tasklet = (
            await client.post("/api/tasklets", json={"title": "test", "prompt": "Read files"})
        ).json()
        path = f"/api/tasklets/{tasklet['id']}/messages"
        assert (await client.post(path, json={"content": "/goal Write code"})).status_code == 422
        await client.patch(
            "/api/settings", json={"execution_mode": "codex", "working_directory": str(project)}
        )
        assert (await client.post(path, json={"content": "/plan Write code"})).status_code == 422
        response = await client.post(path, json={"content": "/goal"})
        assert response.status_code == 202
        assert runtime.store.messages(tasklet["id"])[-1]["content"] == "Цель: тестовая цель"
        assert runtime.worker is None


def test_existing_settings_are_migrated_without_overwriting_data(tmp_path):
    store = Store(tmp_path)
    store.connection.execute(
        "UPDATE settings SET data=? WHERE id=1",
        (
            '{"api_key":"kept-key","model":"kept-model","base_url":"https://example.com/v1","max_parallel":2,"workspace_context":"kept-context"}',
        ),
    )
    store.connection.commit()
    store.close()
    store = Store(tmp_path)
    try:
        assert store.settings()["execution_mode"] == "api"
        assert store.settings()["working_directory"] is None
        assert store.settings(private=True)["api_key"] == "kept-key"
        assert store.settings()["model"] == "kept-model"
    finally:
        store.close()
