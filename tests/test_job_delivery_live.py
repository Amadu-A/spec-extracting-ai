# tests/test_job_delivery_live.py
"""Реальные PostgreSQL/AMQP в выделенном Stage 03A QA, VLM заменён синтетическим ответом."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from app.core.settings import Settings
from app.domain.models import JobState, utcnow
from app.infrastructure.broker import RabbitPublisher
from app.infrastructure.database import SqlRepository
from app.worker import consume_delivery, dispatch_once
from tests.conftest import uploaded
from tests.test_invoices import BASE, HEADERS

pytestmark = pytest.mark.live


def migrate(repo, target):
    """Выполняет штатный Alembic на временной схеме."""
    with repo.engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.upgrade(config, target)


@pytest.fixture
async def delivery_qa(invoice_root, request, monkeypatch):
    """Допускает только выделенные контейнеры QA и изолирует каждую схему и очередь."""
    if not (request.config.getoption("--broker") and request.config.getoption("--postgres")):
        pytest.skip("Нужны --broker --postgres и контейнеры Stage 03A QA")
    settings = Settings(_env_file=None)
    if (
        settings.database_host != "spec-stage03a-pg"
        or settings.rabbitmq_host != "spec-stage03a-rmq-faults"
        or settings.database_name != "stageqafaults"
        or settings.rabbitmq_vhost != "stageqa"
    ):
        pytest.skip("Stage 03A fault-тест требует отдельные локальные PG/RabbitMQ")
    root = invoice_root
    schema = "stageqa_" + uuid4().hex
    admin = create_engine(settings.database_url)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    repository = SqlRepository(settings.database_url)
    repository.engine.dispose()
    repository.engine = create_engine(
        settings.database_url, connect_args={"options": f"-csearch_path={schema}"}
    )
    settings.rabbitmq_queue = schema
    settings.max_queued_jobs = 2
    broker, consumer = RabbitPublisher(settings), RabbitPublisher(settings)
    for adapter in (
        root.extractions,
        root.invoices,
        root.processor,
        root.invoice_processor,
        root.jobs,
        root.sessions,
    ):
        adapter.repository = repository
    root.repository, root.broker = repository, broker
    root.settings.max_queued_jobs = settings.max_queued_jobs
    clock = utcnow()

    def advance(seconds):
        """Продвигает due для публикации без ожидания backoff; broker продолжает работать реально."""
        nonlocal clock
        clock += timedelta(seconds=seconds)

    monkeypatch.setattr("app.infrastructure.database.utcnow", lambda: max(clock, utcnow()))
    try:
        await broker.connect()
        await consumer.connect()
        yield root, broker, consumer, advance
    finally:
        await asyncio.gather(broker.close(), consumer.close(), return_exceptions=True)
        repository.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


async def ready(broker) -> int:
    """Читает реальный Ready через passive declare, минуя кэш robust-очереди."""
    channel = await broker.channel.get_underlay_channel()
    return (await channel.queue_declare(broker.settings.rabbitmq_queue, passive=True)).message_count


@pytest.mark.parametrize("profile", ["specification", "invoice"])
async def test_real_delivery_crash_early_redelivery_and_checkpoints(
    delivery_qa, invoice_client, pdf_bytes, monkeypatch, profile
):
    """Реальный redelivery приходит при действующем lease, затем PG восстанавливает работу."""
    root, broker, consumer, advance = delivery_qa
    await asyncio.to_thread(migrate, root.repository, "head")
    if profile == "invoice":

        async def upload():
            """Оба параллельных REST клиента загружают одинаковый синтетический PDF."""
            return await invoice_client.post(
                BASE,
                headers={**HEADERS, "Idempotency-Key": "stageqa"},
                files={"files": ("qa.pdf", pdf_bytes, "application/pdf")},
            )

        responses = await asyncio.gather(upload(), upload())
        assert {r.status_code for r in responses} == {202}
        identifiers = {response.json()["job_id"] for response in responses}
        model, processor = root.invoice_processor.model, root.invoice_processor
    else:
        doc = await uploaded(root, pdf_bytes)
        jobs = await asyncio.gather(
            *(root.extractions.start("api:alpha", doc.id, "1-2", "P", "same") for _ in range(2))
        )
        identifiers = {job.id for job in jobs}
        model, processor = root.model, root.processor
    assert len(identifiers) == 1
    assert len(list(root.settings.data_directory.iterdir())) == 1
    identifier = identifiers.pop()
    assert sum(await asyncio.gather(dispatch_once(root), dispatch_once(root))) == 1
    message = await consumer.queue.get(timeout=5)
    checkpoint = asyncio.Event()
    real_edit = root.repository.edit

    async def edited(job_id, change, **kwargs):
        """Останавливает только первый worker сразу после durable первого листа."""
        value = await real_edit(job_id, change, **kwargs)
        if value.pages[0].state == "completed" and not checkpoint.is_set():
            checkpoint.set()
            await asyncio.Future()
        return value

    monkeypatch.setattr(root.repository, "edit", edited)
    # Закрытие TCP возвращает unacked брокеру, независимо от судьбы Python coroutine.
    work = asyncio.create_task(root.jobs.execute(identifier, message.headers["revision"]))
    await asyncio.wait_for(checkpoint.wait(), 20)
    await consumer.close()
    work.cancel()
    await asyncio.gather(work, return_exceptions=True)
    await consumer.connect()
    redelivery = await consumer.queue.get(timeout=5)
    assert redelivery.redelivered
    await consume_delivery(root, redelivery)
    assert await ready(broker) == 0
    assert await dispatch_once(root) == 0
    lost = await root.repository.get(identifier)

    def expire(job):
        """Сдвигает только старый lease и проверяет fencing прежнего токена."""
        job.lease_until = utcnow() - timedelta(seconds=1)

    await real_edit(identifier, expire)
    advance(1)
    assert sum(await asyncio.gather(dispatch_once(root), dispatch_once(root))) == 1
    assert await dispatch_once(root) == 0
    assert await ready(broker) == 1
    recovered = await consumer.queue.get(timeout=5)
    await consume_delivery(root, recovered)
    assert len(model.calls) == (3 if profile == "invoice" else 2)
    completed = await root.repository.get(identifier)
    assert completed.state in (JobState.COMPLETED, JobState.WARNINGS)
    assert completed.started_at == lost.started_at
    await processor._finish(identifier, lost.lease_token, JobState.FAILED)
    assert (await root.repository.get(identifier)).state == completed.state
    events = await root.repository.events(identifier, 0, 1000)
    assert sum(event.type == "job_completed" for event in events) == 1
    assert await ready(broker) == 0
    print(
        f"profile={profile} job={identifier} publish=2 confirm=2 redelivery=1 ack=2 external_double_calls={len(model.calls)}"
    )


async def test_real_overflow_unroutable_lost_confirm_and_crash_before_commit(
    delivery_qa, pdf_bytes, monkeypatch
):
    """Переполнение и несуществующий маршрут дают retry; потерянный confirm не удваивает результат."""
    root, broker, consumer, advance = delivery_qa
    repo = root.repository
    await asyncio.to_thread(migrate, repo, "head")
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    for _ in range(broker.settings.max_queued_jobs * 2):
        await broker.publish(str(uuid4()))
    assert await ready(broker) == 4
    assert await dispatch_once(root) == 0
    assert (await repo.delivery_inventory(root.settings.retention_seconds))["counts"][
        "pending"
    ] == 1
    assert await dispatch_once(root) == 0
    for _ in range(4):
        await consume_delivery(root, await consumer.queue.get(timeout=5))
    assert await ready(broker) == 0
    publish = broker.publish

    async def unroutable(identifier, revision):
        """Default exchange возвращает mandatory сообщение с несуществующим routing key."""
        queue = broker.settings.rabbitmq_queue
        broker.settings.rabbitmq_queue += "-missing"
        try:
            await publish(identifier, revision)
        finally:
            broker.settings.rabbitmq_queue = queue

    advance(61)
    monkeypatch.setattr(broker, "publish", unroutable)
    assert await dispatch_once(root) == 0 and await ready(broker) == 0

    async def lost_confirm(identifier, revision):
        """Точно воспроизводит границу неизвестного результата после приёма RabbitMQ."""
        await publish(identifier, revision)
        raise TimeoutError

    advance(61)
    monkeypatch.setattr(broker, "publish", lost_confirm)
    assert await dispatch_once(root) == 0 and await ready(broker) == 1
    monkeypatch.setattr(broker, "publish", publish)
    advance(61)
    abandoned = await repo.claim_delivery(root.settings.retention_seconds)
    await publish(abandoned.job_id, abandoned.revision)
    assert await dispatch_once(root) == 0 and await ready(broker) == 2
    advance(31)
    assert await dispatch_once(root) == 1 and await ready(broker) == 3
    assert not await repo.finish_delivery(abandoned, None)
    for _ in range(3):
        await consume_delivery(root, await consumer.queue.get(timeout=5))
    assert (await repo.get(job.id)).state == JobState.COMPLETED
    assert len(root.model.calls) == 1 and await ready(broker) == 0
    print(
        f"job={job.id} attempts=5 accepted=3 durable_confirm=1 retry=3 ack=3 external_double_calls=1"
    )


async def test_real_migration_drains_legacy_and_reconciles_missing(delivery_qa, pdf_bytes):
    """Миграция сохраняет существующий Rabbit backlog и отдельно восстанавливает отсутствующие UUID."""
    root, broker, consumer, advance = delivery_qa
    repo = root.repository
    await asyncio.to_thread(migrate, repo, "head")
    doc = await uploaded(root, pdf_bytes)
    present = await root.extractions.start("api:alpha", doc.id, "1", "P")
    missing = await root.extractions.start("api:alpha", doc.id, "1", "P")
    with repo.engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.downgrade(config, "0001")
    for _ in range(2):
        await broker.publish(present.id)
    await asyncio.to_thread(migrate, repo, "head")
    assert await dispatch_once(root) == 0 and await ready(broker) == 2
    for _ in range(2):
        await consume_delivery(root, await consumer.queue.get(timeout=5))
    assert len(root.model.calls) == 1
    assert await repo.reconcile_deliveries(
        [present.id, missing.id], root.settings.retention_seconds
    ) == [missing.id]
    assert await repo.reconcile_deliveries([missing.id], root.settings.retention_seconds) == []
    assert await dispatch_once(root) == 1
    assert await repo.reconcile_deliveries([missing.id], root.settings.retention_seconds) == []
    gone = await consumer.queue.get(timeout=5)
    await gone.ack()
    advance(301)
    assert await dispatch_once(root) == 0 and await ready(broker) == 0
    assert await repo.reconcile_deliveries([missing.id], root.settings.retention_seconds) == [
        missing.id
    ]
    assert await dispatch_once(root) == 1
    await consume_delivery(root, await consumer.queue.get(timeout=5))
    assert len(root.model.calls) == 2 and await ready(broker) == 0
    assert (await repo.get(missing.id)).state == JobState.COMPLETED
    print(
        f"legacy_present={present.id} legacy_missing={missing.id} legacy_acks=2 repaired_confirms=2 repaired_acks=1 external_double_calls=2"
    )
