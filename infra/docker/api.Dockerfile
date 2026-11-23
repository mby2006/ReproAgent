FROM python:3.12-slim

ARG PYPI_INDEX=https://pypi.org/simple

# uv 本身就是 PyPI 上的一个 wheel。这里原先从 ghcr.io/astral-sh/uv 单独 COPY 一个阶段，
# 但大陆机房到 ghcr 实测只有 9KB/s（一个 19.6MB 的层要 40 分钟），而同一台机器上 PyPI 正常。
# 少一个镜像源依赖，构建就不可能被它卡住。
RUN pip install --no-cache-dir --index-url ${PYPI_INDEX} uv

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