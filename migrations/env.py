# migrations/env.py
"""Alembic читает те же layered Settings, не печатает DSN с паролем."""

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.settings import Settings
from app.infrastructure.database import Base


def run() -> None:
    """Применяет версионированные миграции только к собственной базе приложения."""
    provided = context.config.attributes.get("connection")
    if provided is not None:
        context.configure(connection=provided, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()
        return
    settings = Settings()
    engine = create_engine(settings.database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=Base.metadata)
        with context.begin_transaction():
            context.run_migrations()


run()
