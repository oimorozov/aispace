import asyncio
import json
import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import Field

from .github_api import GitHubIssueReference
from .import_plan import (
    graph_positions,
    known_graph,
    parse_suggestions,
    planner_input,
    validate_edits,
)
from .import_store import canonical, operation_hash
from .models import InputModel, Workspace
from .planner import GraphPlanner
from .provider import ProviderError


class PlanCreate(InputModel):
    selection_id: str = Field(min_length=1, max_length=100)
    mode: Literal["ai", "known"] = "ai"


class SelectionRevision(InputModel):
    issues: list[GitHubIssueReference] = Field(min_length=1, max_length=1000)


class PlanEdge(InputModel):
    source: int = Field(gt=0, strict=True)
    target: int = Field(gt=0, strict=True)
    explanation: str = Field(default="", max_length=2000)


class DependencyDecision(InputModel):
    kind: Literal["external_completed", "ignore_github"]
    source: int = Field(gt=0, strict=True)
    target: int = Field(gt=0, strict=True)
    reason: str = Field(min_length=1, max_length=2000)


class PlanEdit(InputModel):
    edges: list[PlanEdge] = Field(max_length=10000)
    decisions: list[DependencyDecision] = Field(default_factory=list, max_length=10000)


class ImportCreate(PlanEdit):
    plan_id: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=16, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    working_directory: str | None = Field(default=None, max_length=4096)


def snapshot_signature(selection):
    issues = []
    for original in selection["issues"]:
        issue = json.loads(json.dumps(original))
        issue["labels"] = sorted(issue["labels"], key=lambda label: (label["name"], label["color"]))
        issue["dependencies"]["blocked_by"] = sorted(
            issue["dependencies"]["blocked_by"], key=lambda blocker: blocker["id"]
        )
        issues.append(issue)
    return canonical(
        {
            "repository": {key: selection["repository"][key] for key in ("id", "full_name")},
            "issues": sorted(issues, key=lambda issue: issue["id"]),
        }
    )


class ImportService:
    def __init__(self, runtime, github, planner=None):
        self.runtime = runtime
        self.store = runtime.store
        self.github = github
        self.planner = planner or GraphPlanner()
        self.jobs = {}
        self.lock = asyncio.Lock()

    def get(self, plan_id, completed=False):
        plan = self.store.imports.plan(plan_id)
        if plan is None:
            raise HTTPException(404, "План не найден. Повторите анализ выбранных issues.")
        if completed and plan["status"] != "completed":
            raise HTTPException(409, "План ещё не готов или отменён. Повторите анализ.")
        return plan

    def start(self, body):
        selection = self.store.github_selection(body.selection_id)
        if selection is None:
            raise HTTPException(404, "Выбор issues не найден. Выберите задачи заново.")
        known, external = known_graph(selection)
        data = planner_input(selection) if body.mode == "ai" else None
        plan = {
            "id": uuid4().hex,
            "status": "running",
            "selection_id": selection["id"],
            "selection": selection,
            "edges": known,
            "external_blockers": external,
            "positions": graph_positions(selection, known),
            "error": None,
        }
        self.store.imports.save_plan(plan)
        settings = self.store.settings(private=True)
        task = asyncio.create_task(self._analyze(plan, body.mode, settings, data))
        self.jobs[plan["id"]] = task
        task.add_done_callback(lambda _: self.jobs.pop(plan["id"], None))
        return plan

    async def _analyze(self, plan, mode, settings, data):
        try:
            if mode == "ai":
                content = await self.planner.analyze(settings, data)
                plan.update(parse_suggestions(plan["selection"], content))
            plan["status"] = "completed"
        except asyncio.CancelledError:
            plan.update(status="cancelled", error="Анализ отменён.")
            raise
        except (HTTPException, ProviderError) as error:
            plan.update(
                status="failed",
                error=error.detail if isinstance(error, HTTPException) else str(error),
            )
        except (ValueError, TypeError, KeyError, RuntimeError, OSError, sqlite3.Error):
            plan.update(status="failed", error="Не удалось построить граф. Повторите анализ.")
        finally:
            self.store.imports.save_plan(plan)

    async def cancel(self, plan_id):
        plan = self.get(plan_id)
        task = self.jobs.get(plan_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        plan = self.get(plan_id)
        if plan["status"] != "cancelled":
            plan.update(status="cancelled", error="Анализ отменён.")
            self.store.imports.save_plan(plan)
        return plan

    async def close(self):
        jobs = list(self.jobs.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)

    def validate(self, plan_id, body):
        return validate_edits(self.get(plan_id, completed=True), **body.model_dump())

    async def revise_selection(self, selection_id, body):
        previous = self.store.github_selection(selection_id)
        if previous is None:
            raise HTTPException(404, "Выбор issues не найден. Выберите задачи заново.")
        selection = await self.github.bounded(
            self.github.selection(
                previous["repository"]["full_name"],
                previous["repository"]["id"],
                body.issues,
                persist=False,
            )
        )
        selected = {issue["id"] for issue in selection["issues"]}
        selection["history"] = [
            *previous.get("history", []),
            *[
                {
                    "kind": "excluded_issue",
                    "issue_id": issue["id"],
                    "number": issue["number"],
                    "title": issue["title"],
                    "previous_selection_id": previous["id"],
                    "reason": "Задача исключена пользователем из импорта.",
                    "dependencies": issue["dependencies"],
                }
                for issue in previous["issues"]
                if issue["id"] not in selected
            ],
        ]
        self.store.save_github_selection(selection)
        return selection

    async def create(self, body):
        payload = body.model_dump()
        async with self.lock:
            existing = self.store.imports.existing(body.operation_id, operation_hash(payload))
            if existing is not None:
                return existing
            plan = self.get(body.plan_id, completed=True)
            graph = validate_edits(plan, payload["edges"], payload["decisions"])
            if graph["external_blockers"]:
                raise HTTPException(
                    422, "Сначала разрешите все внешние зависимости выбранных issues."
                )
            if body.working_directory:
                payload["working_directory"] = str(
                    self.runtime.directories.resolve(body.working_directory)
                )
                if payload["working_directory"] != body.working_directory:
                    raise HTTPException(422, "Выберите абсолютный путь к рабочей папке.")
            selection = plan["selection"]
            fresh = await self.github.bounded(
                self.github.selection(
                    selection["repository"]["full_name"],
                    selection["repository"]["id"],
                    [
                        GitHubIssueReference(id=issue["id"], number=issue["number"])
                        for issue in selection["issues"]
                    ],
                    persist=False,
                )
            )
            known_graph(fresh)
            if snapshot_signature(fresh) != snapshot_signature(selection):
                raise HTTPException(
                    409, "Issues или зависимости изменились. Перечитайте выбор и пересчитайте граф."
                )
            plan = self.get(body.plan_id, completed=True)
            graph = validate_edits(plan, payload["edges"], payload["decisions"])
            try:
                workspace = self.store.imports.create(payload, plan, graph)
            except sqlite3.Error as error:
                raise HTTPException(
                    500, "Не удалось сохранить импорт. Повторите тот же запрос."
                ) from error
            self.runtime.changed(workspace["id"])
            return workspace


def import_router(current):
    router = APIRouter(prefix="/api/github")

    @router.post("/selections/{selection_id}/revise", status_code=201)
    async def revise(selection_id: str, body: SelectionRevision):
        return await current().revise_selection(selection_id, body)

    @router.post("/plans", status_code=202)
    async def plan(body: PlanCreate):
        return current().start(body)

    @router.get("/plans/{plan_id}")
    async def get(plan_id: str):
        return current().get(plan_id)

    @router.delete("/plans/{plan_id}")
    async def cancel(plan_id: str):
        return await current().cancel(plan_id)

    @router.post("/plans/{plan_id}/validate")
    async def validate(plan_id: str, body: PlanEdit):
        return current().validate(plan_id, body)

    @router.post("/imports", response_model=Workspace, status_code=201)
    async def create(body: ImportCreate):
        return await current().create(body)

    return router
