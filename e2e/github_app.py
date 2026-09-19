import os

from aispace.github import GitHubClient
from aispace.main import create_app
from fastapi import APIRouter

app = create_app(
    github_client_factory=lambda store: GitHubClient(
        store, base_url=os.environ["AISPACE_GITHUB_MOCK_URL"]
    )
)

fixtures = APIRouter(prefix="/api/e2e")


def import_counts():
    connection = app.state.runtime.store.connection
    return {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
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


@fixtures.get("/import-counts")
async def counts():
    return import_counts()


@fixtures.post("/import-failure")
async def import_failure(body: dict[str, bool]):
    connection = app.state.runtime.store.connection
    enabled = body.get("enabled", False)
    with connection:
        connection.execute("DROP TRIGGER IF EXISTS e2e_import_failure")
        if enabled:
            connection.execute(
                "CREATE TRIGGER e2e_import_failure BEFORE INSERT ON tasklet_sources "
                "BEGIN SELECT RAISE(ABORT,'E2E import transaction failure'); END"
            )
    return {"enabled": enabled, "counts": import_counts()}


app.router.routes[0:0] = fixtures.routes
