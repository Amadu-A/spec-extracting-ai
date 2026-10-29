# Dockerfile
# Только приложение и PDF-парсер. Модели и broker принадлежат shared stack.
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY prompts ./prompts
COPY migrations ./migrations
COPY alembic.ini .env.example ./
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app && mkdir /app/data && chown app:app /app/data
USER app
EXPOSE 8083
CMD ["python", "-m", "app"]
