# tests/test_job_delivery.py
"""Жизненный цикл доставки и обе ревизии API с реальными транзакциями SQLite."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.domain.errors import ServiceError
from app.domain.models import JobState, utcnow
from app.worker import dispatch_once
from tests.conftest import KEY_B, uploaded
from tests.test_invoices import BASE, HEADERS


@pytest.fixture
def delivery_clock(monkeypatch):
    """Продвигает время только транспортной БД без ожидания минуты в offline-тестах."""
    now = utcnow()
    monkeypatch.setattr("app.infrastructure.database.utcnow", lambda: now)

    def advance(seconds):
        """Переводит часы, сохраняя исходный TTL и запись брокера."""
        nonlocal now
        now += timedelta(seconds=seconds)

    return advance


async def test_wait_has_one_confirm_across_ten_intervals_and_dispatchers(
    root, pdf_bytes, delivery_clock
):
    """Три операции ждут десять прежних интервалов и сохраняют единственную доставку."""
    doc = await uploaded(root, pdf_bytes)
    jobs = [await root.extractions.start("api:alpha", doc.id, "1", "P") for _ in range(3)]
    root.broker.publish = AsyncMock()
    delivery_clock(1)
    await asyncio.gather(*(dispatch_once(root) for _ in range(3)))
    for _ in range(10):
        delivery_clock(60)
        assert await dispatch_once(root) == 0
    assert sorted(c.args for c in root.broker.publish.await_args_list) == sorted(
        (j.id, 0) for j in jobs
    )
    inventory = await root.repository.delivery_inventory(root.settings.retention_seconds)
    assert inventory["counts"]["published"] == 3
    assert all(j["attempts"] == 1 and j["confirmed_at"] for j in inventory["jobs"])
    assert all(
        not {"filename", "prompt", "owner", "lease_token"} & j.keys() for j in inventory["jobs"]
    )


async def test_confirm_lost_claim_recovers_and_stale_confirm_cannot_win(
    root, pdf_bytes, delivery_clock
):
    """Потеря confirm, смерть sender и замена claim журналируются без нового бизнес-поколения."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    root.broker.publish = AsyncMock(side_effect=TimeoutError)
    delivery_clock(1)
    assert await dispatch_once(root) == 0
    root.broker.publish.assert_awaited_once()
    assert await dispatch_once(root) == 0
    delivery_clock(10)
    repo = root.repository
    abandoned = await repo.claim_delivery(root.settings.retention_seconds)
    assert abandoned.attempt == 2
    delivery_clock(31)
    claim = await repo.claim_delivery(root.settings.retention_seconds)
    assert (
        claim.attempt == 3
        and claim.revision == abandoned.revision
        and claim.token != abandoned.token
    )
    assert not await repo.finish_delivery(abandoned, None)
    assert await repo.finish_delivery(claim, None)
    assert await root.processor.execute(job.id, 0)
    assert not await root.processor.execute(job.id, 0)
    assert len(root.model.calls) == 1


@pytest.mark.parametrize("profile", ["specification", "invoice"])
async def test_redelivery_with_live_lease_then_cancel_or_recovery(
    profile, invoice_root, invoice_client, pdf_bytes
):
    """Ранний ACK дубля безопасен, а stale cancel и новый retry сохраняют fencing."""
    root = invoice_root
    if profile == "invoice":
        response = await invoice_client.post(
            BASE, headers=HEADERS, files={"files": ("a.pdf", pdf_bytes, "application/pdf")}
        )
        identifier = response.json()["job_id"]
        service = root.invoices
    else:
        doc = await uploaded(root, pdf_bytes)
        identifier = (await root.extractions.start("api:alpha", doc.id, "1", "P")).id
        service = root.extractions

    def claim(job):
        """Воспроизводит durable lease, оставленный аварийной остановкой исполнителя."""
        job.transition(JobState.RUNNING)
        job.lease_token, job.lease_until = "lost-worker", utcnow() + timedelta(seconds=60)

    def expire(job):
        """Ускоряет истечение lease в тестовой базе."""
        job.lease_until = utcnow() - timedelta(seconds=1)

    original = await root.repository.edit(identifier, claim)
    assert original.started_at is not None
    assert not await root.jobs.execute(identifier, 0)
    assert await root.repository.claim_delivery(root.settings.retention_seconds) is None
    await root.repository.edit(identifier, expire)
    lease_recovery = await root.repository.claim_delivery(root.settings.retention_seconds)
    assert lease_recovery.job_id == identifier and await root.repository.finish_delivery(
        lease_recovery, None
    )
    assert await root.repository.claim_delivery(root.settings.retention_seconds) is None
    first = await service.cancel(identifier, "api:alpha")
    second = await service.cancel(identifier, "api:alpha")
    assert first.cancel_requested and first.event_seq == second.event_seq
    await root.extractions.purge_expired()
    cancelled = await root.repository.get(identifier)
    assert cancelled.state == JobState.CANCELLED
    assert service.status(cancelled)["started_at"] == original.started_at.isoformat()
    assert not (root.model.calls or root.invoice_processor.model.calls)
    if profile == "invoice":
        repeated = await service.retry_file(identifier, "api:alpha", 0, 1)
    else:
        repeated = await service.retry(identifier, "api:alpha", 1)
    assert repeated.revision == 1 and service.status(repeated)["started_at"] is None
    assert not await root.jobs.execute(identifier, 0)
    assert not await root.jobs.execute(identifier, -1)
    assert not await root.repository.finish_delivery(lease_recovery, None)
    assert await root.jobs.execute(identifier, 1)
    assert (await root.repository.get(identifier)).state in (JobState.COMPLETED, JobState.WARNINGS)


async def test_retry_cannot_exceed_admission_limit(root, pdf_bytes):
    """Новый retry учитывается в общем лимите и при отказе не меняет ревизию."""
    doc = await uploaded(root, pdf_bytes)
    root.settings.max_queued_jobs = 1
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    await root.extractions.cancel(job.id, "api:alpha")
    await root.extractions.start("api:alpha", doc.id, "1", "P")
    with pytest.raises(ServiceError) as error:
        await root.extractions.retry(job.id, "api:alpha", 1)
    assert error.value.code == "queue_full"
    unchanged = await root.repository.get(job.id)
    assert unchanged.revision == 0 and unchanged.state == JobState.CANCELLED


async def test_uncertain_invoice_admission_keeps_committed_original(
    invoice_root, pdf_bytes, monkeypatch
):
    """Disconnect после commit не удаляет файл, на который уже ссылается durable job."""
    from app.application.invoices.ports import Upload

    root = invoice_root
    actual_create = root.repository.create
    committed = []

    async def create(job, capacity):
        """Разрывает ответ на границе между транзакцией и клиентом."""
        committed.append(await actual_create(job, capacity))
        raise asyncio.CancelledError

    async def chunks():
        """Даёт тот же первоначальный PDF для повтора по стабильному ключу."""
        yield pdf_bytes

    monkeypatch.setattr(root.repository, "create", create)
    with pytest.raises(asyncio.CancelledError):
        await root.invoices.create(
            "api:alpha", [Upload("a.pdf", "application/pdf", chunks())], {}, "lost"
        )
    monkeypatch.setattr(root.repository, "create", actual_create)
    repeated = await root.invoices.create(
        "api:alpha", [Upload("a.pdf", "application/pdf", chunks())], {}, "lost"
    )
    assert repeated.id == committed[0].id
    assert len(list(root.settings.data_directory.iterdir())) == 1
    assert await root.jobs.execute(repeated.id, 0)
    assert (await root.repository.get(repeated.id)).state == JobState.COMPLETED


@pytest.mark.parametrize("profile", ["specification", "invoice"])
async def test_running_cancel_status_snapshot_and_sse_are_owner_scoped(
    profile,
    invoice_root,
    invoice_client,
    pdf_bytes,
    monkeypatch,
):
    """Отмена во время работы видна через API и cursor replay, итог старого worker закрыт."""
    root = invoice_root
    if profile == "invoice":
        response = await invoice_client.post(
            BASE, headers=HEADERS, files={"files": ("qa.pdf", pdf_bytes, "application/pdf")}
        )
        identifier = response.json()["job_id"]
        base, processor = BASE, root.invoice_processor
    else:
        doc = await uploaded(root, pdf_bytes)
        identifier = (await root.extractions.start("api:alpha", doc.id, "1-2", "P")).id
        base, processor = "/api/v1/extractions", root.processor
    job_url = f"{base}/{identifier}"
    entered, resume = asyncio.Event(), asyncio.Event()

    async def pause_before_commit(job_id, lease):
        """Держит фактический worker lease, пока клиент отменяет job."""
        entered.set()
        await resume.wait()
        await processor._finish(job_id, lease, JobState.COMPLETED)

    monkeypatch.setattr(processor, "_process", pause_before_commit)
    before = (await invoice_client.get(job_url, headers=HEADERS)).json()
    assert before["status"] == "queued" and before["started_at"] is None
    work = asyncio.create_task(root.jobs.execute(identifier, 0))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert (
            await invoice_client.post(job_url + "/cancel", headers={"X-API-Key": KEY_B})
        ).status_code == 404
        requested = (await invoice_client.post(job_url + "/cancel", headers=HEADERS)).json()
        assert (
            requested["status"] == "running"
            and requested["cancel_requested"]
            and requested["started_at"]
        )
        snapshot = (await invoice_client.get(job_url + "/snapshot", headers=HEADERS)).json()
        assert snapshot["status"]["cancel_requested"] and snapshot["status"]["processed_pages"] == 0
        assert not {"lease_token", "prompt", "owner"} & snapshot["status"].keys()
        again = (await invoice_client.post(job_url + "/cancel", headers=HEADERS)).json()
        assert requested["last_event_id"] == again["last_event_id"]
    finally:
        resume.set()
        await work
    replay = await invoice_client.get(
        job_url + "/events", headers={**HEADERS, "Last-Event-ID": str(requested["last_event_id"])}
    )
    assert replay.status_code == 200 and "event: job_cancelled" in replay.text
    assert (
        "event: cancellation_requested" not in replay.text
        and "event: job_completed" not in replay.text
    )
    assert (await invoice_client.get(job_url + "/result.json", headers=HEADERS)).status_code == 409
