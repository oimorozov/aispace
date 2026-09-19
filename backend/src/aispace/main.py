import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .codex import CodexProvider
from .directories import Directories
from .github import GitHubClient
from .github_api import github_router
from .import_api import ImportService, import_router
from .models import (
    DirectoryChoose,
    Edge,
    EdgeCreate,
    EdgePatch,
    LoginCancel,
    Message,
    MessageCreate,
    Pipeline,
    PipelineStart,
    Settings,
    SettingsPatch,
    Tasklet,
    TaskletCreate,
    TaskletPatch,
    Workspace,
    WorkspaceCreate,
    WorkspacePatch,
    WorkspaceSummary,
)
from .provider import ChatProvider, ProviderError
from .runtime import Runtime
from .storage import Store


def create_app(
    data_dir=None,
    provider=None,
    codex_provider=None,
    workspace_root=None,
    frontend_dir=None,
    github_client_factory=GitHubClient,
    graph_planner=None,
):
    @asynccontextmanager
    async def lifespan(application):
        directory = Path(
            data_dir
            or os.environ.get("AISPACE_DATA_DIR")
            or Path(__file__).resolve().parents[3] / ".data"
        )
        store = Store(directory)
        directories = Directories(workspace_root)
        codex = codex_provider or CodexProvider(store, directories)
        runtime = Runtime(store, provider or ChatProvider(), codex, directories)
        application.state.runtime = runtime
        application.state.github = github_client_factory(store)
        application.state.imports = ImportService(runtime, application.state.github, graph_planner)
        try:
            yield
        finally:
            await application.state.imports.close()
            await runtime.stop()
            await codex.close()
            await application.state.github.close()
            store.close()

    application = FastAPI(
        title="aispace",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    allowed_origins = {
        f"http://{host}:{port}"
        for host in ("localhost", "127.0.0.1")
        for port in (3000, 4173, 5173, 8000, 8080)
    }
    allowed_origins.update(
        origin.strip().rstrip("/")
        for origin in os.environ.get("AISPACE_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    application.include_router(github_router(lambda: application.state.github))
    application.include_router(import_router(lambda: application.state.imports))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        fields = ", ".join(
            ".".join(str(part) for part in item["loc"][1:]) for item in error.errors()
        )
        return JSONResponse(
            status_code=422, content={"detail": f"Проверьте значения полей: {fields}"}
        )

    @application.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse(status_code=422, content={"detail": str(error)})

    @application.middleware("http")
    async def local_write_guard(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin not in allowed_origins:
                return Response(
                    status_code=403,
                    content='{"detail":"Недопустимый источник запроса"}',
                    media_type="application/json",
                )
        return await call_next(request)

    def runtime():
        return application.state.runtime

    @application.get("/api/health")
    async def health():
        return {"status": "ok"}

    @application.get("/api/workspaces", response_model=list[WorkspaceSummary])
    async def workspaces():
        return runtime().store.workspaces()

    @application.post("/api/workspaces", response_model=Workspace, status_code=201)
    async def create_workspace(body: WorkspaceCreate):
        current = runtime()
        async with current.lock:
            workspace = current.store.create_workspace(body.model_dump())
            current.changed(workspace["id"])
            return workspace

    @application.get("/api/workspaces/{workspace_id}", response_model=Workspace)
    async def workspace(workspace_id: str):
        current = runtime()
        current.require_workspace(workspace_id)
        return current.store.workspace(workspace_id)

    @application.patch("/api/workspaces/{workspace_id}", response_model=Workspace)
    async def update_workspace(workspace_id: str, body: WorkspacePatch):
        current = runtime()
        async with current.lock:
            previous = current.require_workspace(workspace_id)
            current.ensure_idle(workspace_id)
            changes = body.model_dump(exclude_unset=True)
            if changes.get("working_directory"):
                changes["working_directory"] = str(
                    current.directories.resolve(changes["working_directory"])
                )
            if any(
                key in changes and previous[key] != changes[key]
                for key in ("working_directory", "workspace_context")
            ):
                current.invalidate(
                    [tasklet["id"] for tasklet in current.store.tasklets(workspace_id)],
                    workspace_id=workspace_id,
                )
            workspace = current.store.update_workspace(workspace_id, changes)
            current.changed(workspace_id)
            return workspace

    @application.delete("/api/workspaces/{workspace_id}", status_code=204)
    async def delete_workspace(workspace_id: str):
        current = runtime()
        async with current.lock:
            current.require_workspace(workspace_id)
            current.ensure_idle(workspace_id)
            current.store.delete_workspace(workspace_id)
            current.events.publish("workspaces", current.store.workspaces())
            return Response(status_code=204)

    @application.post(
        "/api/workspaces/{workspace_id}/tasklets", response_model=Tasklet, status_code=201
    )
    async def create_tasklet(workspace_id: str, body: TaskletCreate):
        current = runtime()
        async with current.lock:
            current.require_workspace(workspace_id)
            current.ensure_idle(workspace_id)
            if body.working_directory:
                current.directories.resolve(body.working_directory)
            tasklet = current.store.create_tasklet(body.model_dump(), workspace_id=workspace_id)
            current.changed(workspace_id)
            return tasklet

    @application.patch(
        "/api/workspaces/{workspace_id}/tasklets/{tasklet_id}", response_model=Tasklet
    )
    async def update_tasklet(workspace_id: str, tasklet_id: str, body: TaskletPatch):
        current = runtime()
        async with current.lock:
            previous = current.require_tasklet(tasklet_id, workspace_id)
            changes = body.model_dump(exclude_unset=True)
            if set(changes) - {"position"}:
                current.ensure_idle(workspace_id)
            if changes.get("working_directory"):
                changes["working_directory"] = str(
                    current.directories.resolve(changes["working_directory"])
                )
            if any(
                key in changes and previous[key] != changes[key]
                for key in ("prompt", "model", "working_directory")
            ):
                current.invalidate([tasklet_id], workspace_id=workspace_id)
            tasklet = current.store.update_tasklet(tasklet_id, changes, workspace_id=workspace_id)
            current.changed(workspace_id)
            return tasklet

    @application.delete("/api/workspaces/{workspace_id}/tasklets/{tasklet_id}", status_code=204)
    async def delete_tasklet(workspace_id: str, tasklet_id: str):
        current = runtime()
        async with current.lock:
            current.require_tasklet(tasklet_id, workspace_id)
            current.ensure_idle(workspace_id)
            current.invalidate([tasklet_id], workspace_id=workspace_id)
            current.store.delete_tasklet(tasklet_id, workspace_id=workspace_id)
            current.changed(workspace_id)
            return Response(status_code=204)

    @application.post("/api/workspaces/{workspace_id}/edges", response_model=Edge, status_code=201)
    async def create_edge(workspace_id: str, body: EdgeCreate):
        current = runtime()
        async with current.lock:
            current.ensure_idle(workspace_id)
            current.validate_edge(body.source, body.target, workspace_id)
            edge = current.store.create_edge(body.model_dump(), workspace_id=workspace_id)
            current.invalidate([body.target], workspace_id=workspace_id)
            current.changed(workspace_id)
            return edge

    @application.patch("/api/workspaces/{workspace_id}/edges/{edge_id}", response_model=Edge)
    async def update_edge(workspace_id: str, edge_id: str, body: EdgePatch):
        current = runtime()
        async with current.lock:
            previous = current.require_edge(edge_id, workspace_id)
            current.ensure_idle(workspace_id)
            edge = current.store.update_edge(edge_id, body.pass_context, workspace_id=workspace_id)
            if previous["pass_context"] != body.pass_context:
                current.invalidate([edge["target"]], workspace_id=workspace_id)
            current.changed(workspace_id)
            return edge

    @application.delete("/api/workspaces/{workspace_id}/edges/{edge_id}", status_code=204)
    async def delete_edge(workspace_id: str, edge_id: str):
        current = runtime()
        async with current.lock:
            edge = current.require_edge(edge_id, workspace_id)
            current.ensure_idle(workspace_id)
            current.invalidate([edge["target"]], workspace_id=workspace_id)
            current.store.delete_edge(edge_id, workspace_id=workspace_id)
            current.changed(workspace_id)
            return Response(status_code=204)

    @application.post(
        "/api/workspaces/{workspace_id}/tasklets/{tasklet_id}/restart",
        response_model=Pipeline,
        status_code=202,
    )
    async def restart_tasklet(workspace_id: str, tasklet_id: str):
        return await runtime().restart(tasklet_id, workspace_id)

    @application.get(
        "/api/workspaces/{workspace_id}/tasklets/{tasklet_id}/messages",
        response_model=list[Message],
    )
    async def messages(workspace_id: str, tasklet_id: str):
        current = runtime()
        current.require_tasklet(tasklet_id, workspace_id)
        return current.store.messages(tasklet_id, workspace_id=workspace_id)

    @application.post(
        "/api/workspaces/{workspace_id}/tasklets/{tasklet_id}/messages",
        response_model=Pipeline,
        status_code=202,
    )
    async def send_message(workspace_id: str, tasklet_id: str, body: MessageCreate):
        return await runtime().send_message(tasklet_id, body.content, workspace_id)

    @application.get("/api/settings", response_model=Settings)
    async def settings():
        return runtime().store.settings()

    @application.patch("/api/settings", response_model=Settings)
    async def update_settings(body: SettingsPatch):
        current = runtime()
        async with current.lock:
            current.ensure_idle()
            changes = body.model_dump(exclude_unset=True)
            previous = current.store.settings(private=True)
            if any(
                key in changes and previous[key] != changes[key]
                for key in (
                    "execution_mode",
                    "codex_sandbox",
                    "model",
                )
            ):
                for workspace in current.store.workspaces():
                    current.invalidate(
                        [tasklet["id"] for tasklet in current.store.tasklets(workspace["id"])],
                        workspace_id=workspace["id"],
                    )
                    current.changed(workspace["id"])
            settings = current.store.save_settings(changes)
            current.events.publish("settings", settings)
            return settings

    @application.post("/api/settings/test")
    async def test_settings():
        current = runtime()
        settings = current.store.settings(private=True)
        if settings["execution_mode"] == "codex":
            status = await current.codex.status()
            return {"ok": status["authenticated"], "message": status["message"]}
        if not settings["api_key"]:
            return {"ok": False, "message": "Сначала сохраните API-ключ в настройках"}
        return await current.provider.test(settings)

    @application.get("/api/directories")
    async def directories(path: str | None = None):
        return runtime().directories.browse(path)

    @application.get("/api/directories/capabilities")
    async def directory_capabilities():
        return runtime().directories.capabilities()

    @application.post("/api/directories/choose")
    async def choose_directory(body: DirectoryChoose | None = None):
        return await runtime().directories.choose(body.path if body else None)

    @application.get("/api/codex/status")
    async def codex_status():
        return await runtime().codex.status()

    @application.post("/api/codex/login")
    async def codex_login():
        current = runtime()
        async with current.lock:
            current.ensure_idle()
            return await current.codex.login()

    @application.post("/api/codex/login/cancel")
    async def codex_login_cancel(body: LoginCancel):
        await runtime().codex.cancel_login(body.login_id)
        return {"ok": True}

    @application.post("/api/codex/logout")
    async def codex_logout():
        current = runtime()
        async with current.lock:
            current.ensure_idle()
            await current.codex.logout()
            return {"ok": True}

    @application.post(
        "/api/workspaces/{workspace_id}/pipeline/start", response_model=Pipeline, status_code=202
    )
    async def start_pipeline(workspace_id: str, body: PipelineStart | None = None):
        return await runtime().start(body.tasklet_ids if body else None, workspace_id=workspace_id)

    @application.post("/api/workspaces/{workspace_id}/pipeline/stop", response_model=Pipeline)
    async def stop_pipeline(workspace_id: str):
        return await runtime().stop(workspace_id)

    @application.get("/api/events")
    async def events(request: Request):
        current = runtime()
        queue = current.events.subscribe()

        async def stream():
            try:
                yield f"event: workspaces\ndata: {json.dumps(current.store.workspaces(), ensure_ascii=False)}\n\n"
                while not await request.is_disconnected():
                    try:
                        event, data = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": heartbeat\n\n"
            finally:
                current.events.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    static_directory = frontend_dir or os.environ.get("AISPACE_FRONTEND_DIR")
    if static_directory:
        application.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")

    return application


app = create_app()
