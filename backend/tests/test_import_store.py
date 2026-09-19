import copy
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi import HTTPException

from aispace.import_plan import parse_suggestions, validate_edits
from aispace.import_store import operation_hash
from aispace.models import Pipeline
from aispace.storage import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "data")
    yield value
    value.close()


@pytest.fixture
def plan():
    issues = []
    for number, title in enumerate(["Схема **данных**", "API", "UI", "Закрытая документация"], 1):
        issues.append(
            {
                "id": number * 101,
                "number": number,
                "url": f"https://github.com/owner/repo/issues/{number}",
                "title": title,
                "body": f"# Исходный Markdown {number}\n\n  Пробелы.\n```sh\nnpm install\n```\n"
                if number != 4
                else "",
                "state": "closed" if number == 4 else "open",
                "state_reason": "completed" if number == 4 else None,
                "labels": [{"name": "feature", "color": "123456"}],
                "updated_at": "2026-09-19T12:00:00Z",
                "dependencies": {"status": "complete", "blocked_by": [], "error": None},
            }
        )
    issues[1]["dependencies"]["blocked_by"] = [
        {
            "id": 101,
            "number": 1,
            "repository": "owner/repo",
            "title": issues[0]["title"],
            "url": issues[0]["url"],
            "state": "open",
        }
    ]
    selection = {
        "id": "selection-id",
        "repository": {
            "id": 55,
            "full_name": "owner/repo",
            "url": "https://github.com/owner/repo",
            "private": False,
        },
        "issues": issues,
        "created_at": "2026-09-19T13:00:00Z",
        "history": [
            {
                "kind": "excluded_issue",
                "issue_id": 505,
                "number": 5,
                "reason": "Импортируется отдельно",
                "previous_selection_id": "previous-selection",
            }
        ],
    }
    graph = parse_suggestions(
        selection,
        json.dumps(
            {
                "edges": [
                    {
                        "source": 202,
                        "target": 303,
                        "origin": "ai",
                        "explanation": "UI использует API",
                    }
                ]
            }
        ),
    )
    return {
        "id": "plan-id",
        "selection_id": selection["id"],
        "selection": selection,
        "status": "completed",
        "error": None,
        **graph,
    }


def payload_for(plan, **changes):
    return {
        "operation_id": "operation-00000001",
        "plan_id": plan["id"],
        "name": "owner/repo",
        "working_directory": None,
        "edges": [
            {key: edge[key] for key in ("source", "target", "explanation")}
            for edge in plan["edges"]
        ],
        "decisions": [],
        **changes,
    }


def graph_for(plan, payload):
    return validate_edits(plan, payload["edges"], payload["decisions"])


def counts(store):
    return {
        table: store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "workspaces",
            "tasklets",
            "edges",
            "messages",
            "runs",
            "codex_sessions",
            "tasklet_sources",
            "edge_sources",
            "github_imports",
        )
    }


def baseline(store):
    wid = store.default_workspace_id
    task = store.create_tasklet(
        {"title": "Существующая задача", "prompt": "Сохранить прежнюю историю"}, workspace_id=wid
    )
    run = Pipeline(id="original-run", status="completed", total=1, completed=1).model_dump()
    store.save_pipeline(run, workspace_id=wid)
    store.create_message(task["id"], "user", "Прежний вопрос", run["id"], workspace_id=wid)
    store.create_message(task["id"], "assistant", "Прежний ответ", run["id"], workspace_id=wid)
    store.save_codex_session(task["id"], "original-thread", "/fixture/original", workspace_id=wid)
    store.update_tasklet(
        task["id"], {"status": "completed", "last_output": "Прежний ответ"}, workspace_id=wid
    )
    return (
        store.workspace(wid),
        store.messages(task["id"], workspace_id=wid),
        store.codex_session(task["id"], workspace_id=wid),
    )


def test_atomic_import_preserves_exact_sources_prompts_and_idle_state_without_side_effects(
    store, plan, tmp_path
):
    previous, messages, session = baseline(store)
    old_settings = store.settings(private=True)
    old_counts = counts(store)
    folder = tmp_path / "project"
    folder.mkdir()
    marker = folder / "keep.txt"
    marker.write_text("unchanged")
    plan_before = copy.deepcopy(plan)
    store.imports.save_plan(plan)
    payload = payload_for(plan, working_directory=str(folder))
    imported = store.imports.create(payload, plan, graph_for(plan, payload))
    assert imported["id"] != previous["id"]
    assert imported["name"] == "owner/repo"
    assert imported["working_directory"] == str(folder)
    assert imported["workspace_context"] == ""
    assert imported["pipeline"] == Pipeline().model_dump()
    assert len(imported["tasklets"]) == 4
    assert len(imported["edges"]) == 2
    by_issue = {tasklet["source"]["issue"]["id"]: tasklet for tasklet in imported["tasklets"]}
    assert set(by_issue) == {101, 202, 303, 404}
    for original in plan["selection"]["issues"]:
        task = by_issue[original["id"]]
        assert task["title"] == original["title"]
        assert task["prompt"] == (original["body"] or original["title"])
        assert task["source"]["provider"] == "github"
        assert task["source"]["repository"] == plan["selection"]["repository"]
        assert task["source"]["issue"] == original
        assert task["source"]["imported_at"] == imported["created_at"]
        assert task["position"] == plan["positions"][str(original["id"])]
        assert task["status"] == "idle"
        assert task["error"] is None and task["last_output"] == ""
        assert task["conversation_id"] is None
        assert store.messages(task["id"], workspace_id=imported["id"]) == []
        assert store.codex_session(task["id"], workspace_id=imported["id"]) is None
    translated = {(edge["source"], edge["target"]): edge for edge in imported["edges"]}
    for original in plan["edges"]:
        edge = translated[(by_issue[original["source"]]["id"], by_issue[original["target"]]["id"])]
        assert edge["pass_context"] is False
        assert edge["origin"] == original["origin"]
        assert edge["explanation"] == original["explanation"]
    assert all(
        by_issue[404]["id"] not in (edge["source"], edge["target"]) for edge in imported["edges"]
    )
    assert imported["import_metadata"]["selection_id"] == plan["selection"]["id"]
    assert imported["import_metadata"]["plan_id"] == plan["id"]
    assert imported["import_metadata"]["selection_changes"] == plan["selection"]["history"]
    assert store.workspace(previous["id"]) == previous
    previous_task = previous["tasklets"][0]["id"]
    assert store.messages(previous_task, workspace_id=previous["id"]) == messages
    assert store.codex_session(previous_task, workspace_id=previous["id"]) == session
    assert store.settings(private=True) == old_settings
    assert counts(store)["messages"] == old_counts["messages"]
    assert counts(store)["runs"] == old_counts["runs"]
    assert marker.read_text() == "unchanged"
    assert list(folder.iterdir()) == [marker]
    assert plan == plan_before
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("table", ["tasklet_sources", "edges", "edge_sources", "github_imports"])
def test_write_failure_rolls_back_the_entire_import_and_same_request_can_retry(store, plan, table):
    previous, _, _ = baseline(store)
    before = counts(store)
    payload = payload_for(plan)
    graph = graph_for(plan, payload)
    condition = f" WHEN (SELECT COUNT(*) FROM {table}) > 0" if table != "github_imports" else ""
    with store.connection:
        store.connection.execute(
            f"CREATE TRIGGER fail_import BEFORE INSERT ON {table}{condition} BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        store.imports.create(payload, plan, graph)
    assert counts(store) == before
    assert store.workspace(previous["id"]) == previous
    assert store.imports.existing(payload["operation_id"], operation_hash(payload)) is None
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with store.connection:
        store.connection.execute("DROP TRIGGER fail_import")
    result = store.imports.create(payload, plan, graph)
    assert len(result["tasklets"]) == 4
    assert counts(store)["workspaces"] == before["workspaces"] + 1
    assert counts(store)["tasklet_sources"] == 4
    assert counts(store)["github_imports"] == 1


def test_repeated_operation_and_backend_restart_return_the_same_import(store, plan, tmp_path):
    payload = payload_for(plan)
    graph = graph_for(plan, payload)
    store.imports.save_plan(plan)
    first = store.imports.create(payload, plan, graph)
    committed = counts(store)
    assert store.imports.create(copy.deepcopy(payload), copy.deepcopy(plan), graph) == first
    assert counts(store) == committed
    reordered_payload = dict(reversed(list(payload.items())))
    assert operation_hash(reordered_payload) == operation_hash(payload)
    store.close()
    reopened = Store(tmp_path / "data")
    try:
        assert reopened.imports.plan(plan["id"]) == plan
        assert reopened.imports.create(reordered_payload, plan, graph) == first
        assert reopened.workspace(first["id"]) == first
        assert counts(reopened) == committed
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Другой проект"},
        {"working_directory": "/different/path"},
        {"plan_id": "another-snapshot-plan"},
        {"edges": []},
        {
            "decisions": [
                {"kind": "ignore_github", "source": 101, "target": 202, "reason": "new decision"}
            ]
        },
    ],
)
def test_same_operation_key_with_different_plan_or_parameters_conflicts(store, plan, changes):
    payload = payload_for(plan)
    graph = graph_for(plan, payload)
    first = store.imports.create(payload, plan, graph)
    before = counts(store)
    with pytest.raises(HTTPException) as error:
        store.imports.create({**payload, **changes}, plan, graph)
    assert error.value.status_code == 409
    assert store.workspace(first["id"]) == first
    assert counts(store) == before


def test_new_operation_can_intentionally_import_the_same_sources_into_another_workspace(
    store, plan
):
    payload = payload_for(plan)
    graph = graph_for(plan, payload)
    first = store.imports.create(payload, plan, graph)
    second = store.imports.create({**payload, "operation_id": "operation-00000002"}, plan, graph)
    assert first["id"] != second["id"]
    assert {task["id"] for task in first["tasklets"]}.isdisjoint(
        task["id"] for task in second["tasklets"]
    )
    assert {task["source"]["issue"]["id"] for task in first["tasklets"]} == {
        task["source"]["issue"]["id"] for task in second["tasklets"]
    }
    assert store.workspace(first["id"]) == first
    assert counts(store)["github_imports"] == 2


def test_explicit_dependency_exceptions_are_stored_separately_from_original_sources(store, plan):
    blocker = {"id": 909, "number": 9, "repository": "other/repo", "state": "closed"}
    plan["selection"]["issues"][2]["dependencies"]["blocked_by"] = [blocker]
    decisions = [
        {"kind": "ignore_github", "source": 101, "target": 202, "reason": "Реализовано иначе"},
        {
            "kind": "external_completed",
            "source": 909,
            "target": 303,
            "reason": "Проверено вне пространства",
        },
    ]
    payload = payload_for(plan, edges=[], decisions=decisions)
    graph = graph_for(plan, payload)
    assert graph["external_blockers"] == []
    imported = store.imports.create(payload, plan, graph)
    assert imported["edges"] == []
    assert imported["import_metadata"]["decisions"] == decisions
    sources = {
        task["source"]["issue"]["id"]: task["source"]["issue"] for task in imported["tasklets"]
    }
    assert sources[202]["dependencies"]["blocked_by"][0]["id"] == 101
    assert sources[303]["dependencies"]["blocked_by"] == [blocker]


@pytest.mark.parametrize("single", [False, True])
def test_fresh_chat_reset_preserves_github_sources_edges_and_import_metadata(store, plan, single):
    payload = payload_for(plan)
    imported = store.imports.create(payload, plan, graph_for(plan, payload))
    wid = imported["id"]
    previous_run = Pipeline(id=uuid4().hex, status="completed", total=4, completed=4).model_dump()
    store.save_pipeline(previous_run, workspace_id=wid)
    for task in imported["tasklets"]:
        store.create_message(task["id"], "user", "Old chat", previous_run["id"], workspace_id=wid)
        store.save_codex_session(task["id"], f"thread-{task['id']}", "/fixture", workspace_id=wid)
        store.update_tasklet(
            task["id"], {"status": "completed", "last_output": "Old answer"}, workspace_id=wid
        )
    chosen = [task["id"] for task in imported["tasklets"][: 1 if single else 4]]
    pipeline = Pipeline(id=uuid4().hex, status="running", total=len(chosen)).model_dump()
    store.accept_run(pipeline, chosen, [], reset_context=True, workspace_id=wid)
    updated = store.workspace(wid)
    assert updated["import_metadata"] == imported["import_metadata"]
    assert updated["edges"] == imported["edges"]
    original_sources = {task["id"]: task["source"] for task in imported["tasklets"]}
    for task in updated["tasklets"]:
        assert task["source"] == original_sources[task["id"]]
        if task["id"] in chosen:
            assert store.messages(task["id"], workspace_id=wid) == []
            assert store.codex_session(task["id"], workspace_id=wid) is None
            assert task["conversation_id"] == pipeline["id"]
            assert task["last_output"] == ""
        else:
            assert len(store.messages(task["id"], workspace_id=wid)) == 1
            assert store.codex_session(task["id"], workspace_id=wid) is not None
    assert len(store.runs(wid)) == 2


def test_deleted_import_is_not_recreated_by_retry_and_sources_cascade_only_with_owner(store, plan):
    previous, _, _ = baseline(store)
    payload = payload_for(plan)
    graph = graph_for(plan, payload)
    imported = store.imports.create(payload, plan, graph)
    assert store.delete_workspace(imported["id"]) is True
    assert counts(store)["tasklet_sources"] == counts(store)["edge_sources"] == 0
    assert store.workspace(previous["id"]) == previous
    with pytest.raises(HTTPException) as error:
        store.imports.create(payload, plan, graph)
    assert error.value.status_code == 410
    assert len(store.workspaces()) == 1
    assert counts(store)["github_imports"] == 1
    assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_restart_cancels_only_unfinished_planning_and_keeps_completed_snapshot(
    store, plan, tmp_path
):
    store.imports.save_plan(plan)
    store.imports.save_plan({**plan, "id": "unfinished", "status": "running"})
    before = counts(store)
    store.close()
    reopened = Store(tmp_path / "data")
    try:
        assert reopened.imports.plan(plan["id"]) == plan
        cancelled = reopened.imports.plan("unfinished")
        assert cancelled["status"] == "cancelled"
        assert "перезапущен" in cancelled["error"]
        assert cancelled["selection"] == plan["selection"]
        assert counts(reopened) == before
    finally:
        reopened.close()
