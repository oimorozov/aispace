import json
from contextlib import asynccontextmanager

import httpx
import pytest

from aispace.github import GitHubClient, repository_name
from aispace.main import create_app
from aispace.storage import Store


def issue(number, repository="owner/project", **changes):
    return {
        "id": 1000 + number,
        "number": number,
        "title": f"Issue {number}",
        "body": f"Full **Markdown** #{number}\n\n" + "content\n" * 300,
        "state": "open",
        "state_reason": None,
        "labels": [{"name": "feature", "color": "abcdef"}],
        "updated_at": "2026-09-19T12:00:00Z",
        "repository_url": f"https://api.github.com/repos/{repository}",
        **changes,
    }


class GitHubFixture:
    def __init__(self):
        self.requests = []
        self.failure = None
        self.dependency_failure = False
        self.bad_link = False

    def handle(self, request):
        self.requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.method == "GET"
        if self.failure:
            if self.failure == "timeout":
                raise httpx.ReadTimeout("secret-fixture-token", request=request)
            return httpx.Response(self.failure, json={"message": "secret-fixture-token"})
        path = request.url.path
        page = request.url.params.get("page", "1")
        if path in {"/repos/owner/project", "/repos/owner/private", "/repos/other/project"}:
            private = path.endswith("private")
            if private and request.headers.get("authorization") != "Bearer secret-fixture-token":
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": 2 if private else 1 if "/owner/" in path else 3,
                    "full_name": path.removeprefix("/repos/"),
                    "private": private,
                },
            )
        if path.endswith("/dependencies/blocked_by"):
            if page == "2":
                if self.dependency_failure:
                    return httpx.Response(403, json={"message": "secret-fixture-token"})
                return httpx.Response(200, json=[issue(9, "other/project", id=9009)])
            return httpx.Response(
                200,
                json=[issue(1)],
                headers={"Link": f'<https://api.github.com{path}?page=2>; rel="next"'},
            )
        if path.endswith("/issues"):
            assert request.url.params["state"] == "all"
            if page == "2":
                return httpx.Response(
                    200, json=[issue(23, body=None, state="closed", state_reason="completed")]
                )
            link = (
                "https://evil.example/steal?page=2"
                if self.bad_link
                else f"https://api.github.com{path}?page=2"
            )
            return httpx.Response(
                200,
                json=[issue(1), issue(2, pull_request={"url": "never-fetch"})],
                headers={"Link": f'<{link}>; rel="next"'},
            )
        if "/issues/" in path:
            number = int(path.rsplit("/", 1)[1])
            if number == 404:
                return httpx.Response(404)
            return httpx.Response(
                200, json=issue(number, pull_request={}) if number == 2 else issue(number)
            )
        return httpx.Response(404)


@asynccontextmanager
async def connected(tmp_path, fixture=None):
    fixture = fixture or GitHubFixture()
    factory = lambda store: GitHubClient(store, transport=httpx.MockTransport(fixture.handle))
    app = create_app(tmp_path, github_client_factory=factory)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client, app, fixture


@pytest.mark.parametrize(
    "value",
    [
        "owner/project",
        "https://github.com/owner/project",
        "https://github.com/owner/project/",
        "owner/project.git",
    ],
)
def test_repository_input_normalizes_supported_identifiers(value):
    assert repository_name(value) == "owner/project"


@pytest.mark.parametrize(
    "value",
    [
        "http://github.com/owner/project",
        "https://evil.example/owner/project",
        "https://github.com@evil.example/owner/project",
        "https://github.com/owner/project/issues",
        "owner/../project",
        "https://github.com/owner/project?token=secret",
        "owner/%2e%2e",
        "owner/..",
        "owner/project#1",
    ],
)
async def test_invalid_repository_never_causes_network_request(tmp_path, value):
    async with connected(tmp_path) as (client, _, fixture):
        response = await client.post("/api/github/repository", json={"repository": value})
        assert response.status_code == 422
        assert fixture.requests == []


async def test_all_pages_pr_filter_raw_body_and_private_token_separation(tmp_path, caplog):
    async with connected(tmp_path) as (client, app, fixture):
        store = app.state.runtime.store
        before = store.workspace()
        public = await client.post(
            "/api/github/repository", json={"repository": "https://github.com/owner/project"}
        )
        assert public.status_code == 200
        data = public.json()
        assert [item["number"] for item in data["issues"]] == [1, 23]
        assert data["issues"][0]["body"] == issue(1)["body"]
        assert data["issues"][1]["body"] == ""
        assert data["issues"][1]["state_reason"] == "completed"
        assert all("authorization" not in request.headers for request in fixture.requests)
        denied = await client.post("/api/github/repository", json={"repository": "owner/private"})
        assert denied.status_code == 404
        assert "либо недоступны" in denied.json()["detail"]
        saved = await client.patch("/api/github/connection", json={"token": "secret-fixture-token"})
        assert saved.json() == {"token_configured": True}
        private = await client.post("/api/github/repository", json={"repository": "owner/private"})
        assert private.status_code == 200
        assert private.json()["repository"]["private"] is True
        outputs = [
            saved.text,
            private.text,
            (await client.get("/api/github/connection")).text,
            (await client.get("/api/settings")).text,
            json.dumps(store.workspace()),
            caplog.text,
        ]
        assert all("secret-fixture-token" not in output for output in outputs)
        assert store.workspace() == before
        assert app.state.runtime.worker is None
        assert store.settings(private=True)["api_key"] is None
    reopened = Store(tmp_path)
    assert reopened.github_token() == "secret-fixture-token"
    reopened.close()


async def test_selection_validates_ids_and_persists_full_snapshot_and_paginated_dependencies(
    tmp_path,
):
    async with connected(tmp_path) as (client, app, fixture):
        body = {
            "repository": "owner/project",
            "repository_id": 1,
            "issues": [{"id": 1023, "number": 23}, {"id": 1023, "number": 23}],
        }
        response = await client.post("/api/github/selections", json=body)
        assert response.status_code == 201
        snapshot = response.json()
        assert len(snapshot["issues"]) == 1
        chosen = snapshot["issues"][0]
        assert chosen["body"] == issue(23)["body"]
        assert chosen["dependencies"]["status"] == "complete"
        assert [item["id"] for item in chosen["dependencies"]["blocked_by"]] == [1001, 9009]
        assert chosen["dependencies"]["blocked_by"][1]["repository"] == "other/project"
        assert app.state.runtime.worker is None
        assert app.state.runtime.store.workspace()["tasklets"] == []
        assert (await client.get(f"/api/github/selections/{snapshot['id']}")).json() == snapshot
        for invalid in [
            {**body, "repository_id": 99},
            {**body, "issues": [{"id": 9999, "number": 23}]},
            {**body, "issues": [{"id": 1002, "number": 2}]},
            {**body, "issues": [{"id": 1404, "number": 404}]},
            {**body, "repository": "other/project", "repository_id": 3},
        ]:
            assert (await client.post("/api/github/selections", json=invalid)).status_code in {
                404,
                409,
                422,
            }
        assert {request.method for request in fixture.requests} == {"GET"}
    reopened = Store(tmp_path)
    assert reopened.github_selection(snapshot["id"]) == snapshot
    assert reopened.connection.execute("SELECT COUNT(*) FROM github_selections").fetchone()[0] == 1
    reopened.close()


async def test_dependency_failure_is_distinct_from_empty_and_preserves_known_pages(tmp_path):
    fixture = GitHubFixture()
    fixture.dependency_failure = True
    async with connected(tmp_path, fixture) as (client, _, _):
        response = await client.post(
            "/api/github/selections",
            json={
                "repository": "owner/project",
                "repository_id": 1,
                "issues": [{"id": 1023, "number": 23}],
            },
        )
        dependency = response.json()["issues"][0]["dependencies"]
        assert dependency["status"] == "unavailable"
        assert dependency["blocked_by"][0]["id"] == 1001
        assert "Issues: read" in dependency["error"]
        assert "secret-fixture-token" not in response.text


@pytest.mark.parametrize(
    ("failure", "status"), [(401, 401), (403, 403), (404, 404), (500, 502), ("timeout", 504)]
)
async def test_access_and_timeout_errors_are_useful_and_never_echo_upstream_secrets(
    tmp_path, failure, status
):
    fixture = GitHubFixture()
    fixture.failure = failure
    async with connected(tmp_path, fixture) as (client, app, _):
        before = app.state.runtime.store.workspace()
        response = await client.post("/api/github/repository", json={"repository": "owner/project"})
        assert response.status_code == status
        assert response.json()["detail"]
        assert "secret-fixture-token" not in response.text
        assert app.state.runtime.store.workspace() == before


async def test_rate_limit_wait_is_enforced_without_retries_then_recovers(tmp_path):
    fixture = GitHubFixture()
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                403,
                headers={"Retry-After": "2"},
                json={"message": "rate limit secret-fixture-token"},
            )
        return fixture.handle(request)

    app = create_app(
        tmp_path,
        github_client_factory=lambda store: GitHubClient(
            store, transport=httpx.MockTransport(handler)
        ),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        for _ in range(2):
            response = await client.post(
                "/api/github/repository", json={"repository": "owner/project"}
            )
            assert response.status_code == 429
            assert response.headers["retry-after"] == "2"
        assert len(calls) == 1
        app.state.github.retry_at = 0
        assert (
            await client.post("/api/github/repository", json={"repository": "owner/project"})
        ).status_code == 200


async def test_untrusted_pagination_url_is_not_followed(tmp_path):
    fixture = GitHubFixture()
    fixture.bad_link = True
    async with connected(tmp_path, fixture) as (client, _, _):
        response = await client.post("/api/github/repository", json={"repository": "owner/project"})
        assert response.status_code == 502
        assert len(fixture.requests) == 2
