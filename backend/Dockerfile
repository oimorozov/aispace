FROM node:22-bookworm-slim AS codex

RUN npm install --global @openai/codex@0.155.0

FROM python:3.13-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.16 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/pyproject.toml
RUN uv sync --frozen --no-dev --no-install-workspace --package aispace-backend

COPY backend/src backend/src
RUN uv sync --frozen --no-dev --no-editable --package aispace-backend

FROM python:3.13-slim AS runtime

ENV PATH="/app/.venv/bin:$PATH" AISPACE_DATA_DIR=/data AISPACE_CODEX_HOME=/data/codex PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN apt-get update && apt-get install --yes --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN useradd --system --uid 10001 --create-home aispace && mkdir /data && chown aispace:aispace /data
COPY --from=build /app/.venv /app/.venv
COPY --from=codex /usr/local/bin/node /usr/local/bin/node
COPY --from=codex /usr/local/lib/node_modules /opt/node_modules
RUN ln -s /opt/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex && ln -s /opt/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm

USER aispace
EXPOSE 8000

CMD ["uvicorn", "aispace.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "25"]
