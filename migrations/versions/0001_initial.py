# migrations/versions/0001_initial.py
"""Начальная схема durable jobs, replay событий, PDF metadata и web-сессий."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт исключительно project-specific таблицы; shared PostgreSQL не затронут."""
    op.create_table(
        "extraction_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(200)),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner", "idempotency_key"),
    )
    for name in ("owner", "state", "expires_at"):
        op.create_index(f"ix_extraction_jobs_{name}", "extraction_jobs", [name])
    op.create_table(
        "extraction_events",
        sa.Column(
            "job_id",
            sa.String(36),
            sa.ForeignKey("extraction_jobs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    for table, size in (("documents", 36), ("web_sessions", 64)):
        op.create_table(
            table,
            sa.Column("id", sa.String(size), primary_key=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
        )
        op.create_index(f"ix_{table}_expires_at", table, ["expires_at"])


def downgrade() -> None:
    """Явный rollback удаляет данные только этих таблиц; оператор делает backup заранее."""
    for table in ("web_sessions", "documents", "extraction_events", "extraction_jobs"):
        op.drop_table(table)
