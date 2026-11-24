# migrations/versions/0002_delivery.py
"""Добавляет durable состояние опубликования без классификации старых сообщений."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

COLUMNS = (
    ("started_at", sa.DateTime(timezone=True)),
    ("delivery_revision", sa.Integer()),
    ("delivery_state", sa.String(16)),
    ("delivery_due", sa.DateTime(timezone=True)),
    ("delivery_claim", sa.String(36)),
    ("delivery_attempts", sa.Integer()),
    ("delivery_confirmed_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    """Берегёт legacy snapshot и отличает неизвестную историческую доставку по NULL."""
    for name, sql_type in COLUMNS:
        op.add_column("extraction_jobs", sa.Column(name, sql_type, nullable=True))
    op.create_index("ix_extraction_jobs_delivery_state", "extraction_jobs", ["delivery_state"])


def downgrade() -> None:
    """Удаляет только транспортные метаданные. Для отката образа миграцию можно оставить."""
    op.drop_index("ix_extraction_jobs_delivery_state", table_name="extraction_jobs")
    for name, _ in reversed(COLUMNS):
        op.drop_column("extraction_jobs", name)
