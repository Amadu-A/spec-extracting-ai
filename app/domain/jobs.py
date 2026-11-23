# app/domain/jobs.py
"""Совместимое чтение legacy snapshot и нового профиля в одной таблице jobs."""

from app.domain.invoices.models import InvoiceJob
from app.domain.models import Job


def read_job(payload: dict) -> Job:
    """Отсутствующий discriminator означает прежнее задание спецификаций."""
    return (InvoiceJob if payload.get("job_type") == "invoice" else Job).model_validate(payload)


def document_ids(job: Job) -> list[str]:
    """Возвращает все оригиналы, защищённые сроком жизни одного задания."""
    return (
        [source.document_id for source in job.sources]
        if isinstance(job, InvoiceJob)
        else [job.document_id]
    )
