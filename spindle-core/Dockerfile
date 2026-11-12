# Stage 1: Build
FROM python:3.14.7-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /uvx /bin/

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /build

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# Stage 2: Runtime
FROM python:3.14.7-slim

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN useradd --system --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin spindle

COPY --from=build /opt/venv /opt/venv

WORKDIR /app

COPY alembic.ini ./
COPY migrations ./migrations

USER 10001:10001

EXPOSE 8000

CMD ["uvicorn", "spindle_core.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

