# Dockerfile
# Только приложение и PDF-парсер. Модели и broker принадлежат shared stack.
# Официальный Python 3.12/Astral в GHCR: сборка не обращается к auth.docker.io.
FROM ghcr.io/astral-sh/uv:0.12.23-python3.12-trixie-slim@sha256:28d570c06d150303b39adfa731ada20956c63fc86b1277d7ff2ad63185974092 AS base
FROM base AS builder
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN umask 022 && uv sync --frozen --no-dev --no-install-project

FROM base AS runtime
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY app ./app
COPY prompts ./prompts
COPY migrations ./migrations
COPY alembic.ini .env.example ./
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app \
    && chmod 0755 /app \
    && find /app/app /app/prompts /app/migrations -type d -exec chmod 0755 {} + \
    && find /app/app /app/prompts /app/migrations -type f -exec chmod 0644 {} + \
    && chmod 0644 /app/alembic.ini /app/.env.example \
    && install -d -o app -g app -m 0700 /app/data
USER app
EXPOSE 8083
CMD ["python", "-m", "app"]
