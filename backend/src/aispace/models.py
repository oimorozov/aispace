from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Position(InputModel):
    x: float = Field(default=0, allow_inf_nan=False)
    y: float = Field(default=0, allow_inf_nan=False)


class TaskletCreate(InputModel):
    title: str = Field(min_length=1, max_length=200)
    prompt: str = Field(default="", max_length=200_000)
    model: str | None = Field(default=None, max_length=200)
    working_directory: str | None = Field(default=None, max_length=4096)
    position: Position = Field(default_factory=Position)


class TaskletPatch(InputModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    prompt: str | None = Field(default=None, max_length=200_000)
    model: str | None = Field(default=None, max_length=200)
    working_directory: str | None = Field(default=None, max_length=4096)
    position: Position | None = None

    @field_validator("title", "prompt", "position")
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise ValueError("Значение не может быть null")
        return value


class EdgeCreate(InputModel):
    source: str
    target: str
    pass_context: bool = False


class EdgePatch(InputModel):
    pass_context: bool


class SettingsPatch(InputModel):
    execution_mode: Literal["api", "codex"] | None = None
    working_directory: str | None = Field(default=None, max_length=4096)
    codex_sandbox: Literal["read-only", "workspace-write"] | None = None
    api_key: str | None = Field(default=None, max_length=4096)
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)
    max_parallel: int | None = Field(default=None, ge=1, le=8)
    workspace_context: str | None = Field(default=None, max_length=200_000)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        if value is None:
            raise ValueError("Укажите адрес API")
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("Нужен HTTP(S) адрес API без пароля, параметров и фрагмента")
        return value.rstrip("/")

    @field_validator(
        "model", "max_parallel", "workspace_context", "execution_mode", "codex_sandbox"
    )
    @classmethod
    def not_null(cls, value):
        if value is None:
            raise ValueError("Значение не может быть null")
        return value


class MessageCreate(InputModel):
    content: str = Field(min_length=1, max_length=200_000)


class LoginCancel(InputModel):
    login_id: str = Field(min_length=1, max_length=200)


class PipelineStart(InputModel):
    tasklet_ids: list[str] | None = Field(default=None, max_length=1000)


TaskStatus = Literal["idle", "queued", "running", "completed", "failed", "cancelled", "blocked"]


class Tasklet(BaseModel):
    id: str
    title: str
    prompt: str
    model: str | None
    working_directory: str | None = None
    status: TaskStatus
    position: Position
    created_at: str
    updated_at: str
    error: str | None
    last_output: str


class Edge(BaseModel):
    id: str
    source: str
    target: str
    pass_context: bool


class Message(BaseModel):
    id: str
    tasklet_id: str
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: str
    run_id: str | None


class Pipeline(BaseModel):
    id: str | None = None
    status: Literal["idle", "running", "stopping", "completed", "failed", "cancelled"] = "idle"
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    completed: int = 0
    error: str | None = None


class Settings(BaseModel):
    execution_mode: Literal["api", "codex"] = "api"
    working_directory: str | None = None
    codex_sandbox: Literal["read-only", "workspace-write"] = "read-only"
    api_key_configured: bool
    base_url: str
    model: str
    max_parallel: int
    workspace_context: str


class Workspace(BaseModel):
    tasklets: list[Tasklet]
    edges: list[Edge]
    pipeline: Pipeline
