from fastapi import APIRouter, HTTPException
from pydantic import Field, field_validator

from .models import InputModel


class GitHubConnectionUpdate(InputModel):
    token: str | None = Field(max_length=4096, repr=False)

    @field_validator("token")
    @classmethod
    def valid_token(cls, value):
        if value is not None and (
            not value or any(not 33 <= ord(character) <= 126 for character in value)
        ):
            raise ValueError("Некорректный токен GitHub")
        return value


class GitHubRepositoryRequest(InputModel):
    repository: str = Field(min_length=1, max_length=250)


class GitHubIssueReference(InputModel):
    id: int = Field(gt=0)
    number: int = Field(gt=0)


class GitHubSelectionRequest(GitHubRepositoryRequest):
    repository_id: int = Field(gt=0)
    issues: list[GitHubIssueReference] = Field(min_length=1)


def github_router(current):
    router = APIRouter(prefix="/api/github")

    @router.get("/connection")
    async def connection():
        return {"token_configured": bool(current().store.github_token())}

    @router.patch("/connection")
    async def update_connection(body: GitHubConnectionUpdate):
        current().store.save_github_token(body.token)
        return {"token_configured": bool(body.token)}

    @router.post("/repository")
    async def repository(body: GitHubRepositoryRequest):
        github = current()
        return await github.bounded(github.list_issues(body.repository))

    @router.post("/selections", status_code=201)
    async def selection(body: GitHubSelectionRequest):
        github = current()
        return await github.bounded(
            github.selection(body.repository, body.repository_id, body.issues)
        )

    @router.get("/selections/{selection_id}")
    async def get_selection(selection_id: str):
        snapshot = current().store.github_selection(selection_id)
        if snapshot is None:
            raise HTTPException(404, "Выбор GitHub Issues не найден. Выберите задачи заново.")
        return snapshot

    return router
