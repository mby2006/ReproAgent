ARG UV_IMAGE=ghcr.io/astral-sh/uv:latest
FROM ${UV_IMAGE} AS uv

FROM python:3.12-slim

ARG PYPI_INDEX=https://pypi.org/simple

COPY --from=uv /uv /uvx /bin/

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_DEFAULT_INDEX=${PYPI_INDEX} \
    UV_HTTP_TIMEOUT=120 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
COPY packages/schemas/pyproject.toml packages/schemas/pyproject.toml
COPY packages/paperparse/pyproject.toml packages/paperparse/pyproject.toml
COPY apps/api/pyproject.toml apps/api/pyproject.toml
COPY apps/agent/pyproject.toml apps/agent/pyproject.toml
COPY apps/runner/pyproject.toml apps/runner/pyproject.toml
COPY packages/schemas packages/schemas
COPY apps/api apps/api

RUN uv sync --frozen --no-dev --package reproagent-api

EXPOSE 8000
CMD ["uvicorn", "reproagent_api.main:app", "--host", "0.0.0.0", "--port", "8000"]