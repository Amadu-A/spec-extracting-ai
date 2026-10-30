# tests/test_live_dependencies.py
"""Опциональные реальные PG/AMQP/vision tests; отсутствие доступа видно как skip, не fake успех."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.application.ports import PreparedPage
from app.core.settings import Settings
from app.domain.models import utcnow
from app.domain.reconcile import parse_model_response
from app.infrastructure.broker import RabbitPublisher
from app.infrastructure.database import SqlRepository
from app.infrastructure.vlm import SharedVlmClient
from tests.conftest import uploaded


@pytest.mark.live
async def test_postgresql_migrations_transactions_locks(root, pdf_bytes, request):
    """Применяет настоящую миграцию и проверяет concurrent idempotency/атомарный replay."""
    if not request.config.getoption("--postgres"):
        pytest.skip("Не указан --postgres")
    settings = Settings()
    schema = "test_" + uuid4().hex
    admin_engine = create_engine(settings.database_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    repository = SqlRepository(settings.database_url)
    repository.engine.dispose()
    repository.engine = create_engine(
        settings.database_url, connect_args={"options": f"-csearch_path={schema}"}
    )

    def migrate():
        """Применяет миграции в отдельной случайной schema, невидимой production dispatcher."""
        with repository.engine.begin() as connection:
            configuration = Config("alembic.ini")
            configuration.attributes["connection"] = connection
            command.upgrade(configuration, "head")

    await asyncio.to_thread(migrate)
    assert await repository.ping()
    tables = inspect(repository.engine).get_table_names()
    assert {
        "documents",
        "extraction_jobs",
        "extraction_events",
        "web_sessions",
        "alembic_version",
    }.issubset(tables)
    root.repository = repository
    root.extractions.repository = repository
    root.processor.repository = repository
    doc = await uploaded(root, pdf_bytes, "live-test")
    key = "live-" + str(uuid4())
    jobs = await asyncio.gather(
        *[root.extractions.start("live-test", doc.id, "1", "P", key) for _ in range(3)]
    )
    assert len({job.id for job in jobs}) == 1
    await asyncio.gather(root.processor.execute(jobs[0].id), root.processor.execute(jobs[0].id))
    current = await repository.get(jobs[0].id)
    assert current.state.value == "completed" and len(root.model.calls) == 1
    events = await repository.events(current.id, 0, 200)
    assert [event.id for event in events] == list(range(1, current.event_seq + 1))
    assert (await repository.document(doc.id)).expires_at == current.expires_at
    second = await root.extractions.start("live-test", doc.id, "2", "P")
    assert (await repository.document(doc.id)).expires_at == second.expires_at
    await root.processor.execute(second.id)
    finished = await repository.get(second.id)
    assert (await repository.document(doc.id)).expires_at == max(
        current.expires_at, finished.expires_at
    )

    def expire(job):
        """Проверяет настоящие JSON-предикаты и FK-очистку только во временной schema."""
        job.expires_at = utcnow() - timedelta(seconds=1)

    for job in (current, finished):
        await repository.edit(job.id, expire)
    assert await repository.cleanup() == [doc.id]
    assert await repository.get(current.id) is None and await repository.get(second.id) is None
    repository.engine.dispose()
    with admin_engine.begin() as connection:
        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin_engine.dispose()


@pytest.mark.live
async def test_existing_shared_broker_confirm_and_delivery(request):
    """Проверяет confirms и получение собственного UUID из app-очереди без admin API."""
    if not request.config.getoption("--broker"):
        pytest.skip("Не указан --broker: нужны app credentials")
    settings = Settings()
    # Отдельное имя тестовой очереди в том же app-vhost исключает перехват production jobs.
    settings.rabbitmq_queue += ".contract-test." + uuid4().hex
    broker = RabbitPublisher(settings)
    try:
        assert await broker.ping()
        identifier = str(uuid4())
        await broker.publish(identifier)
        message = await broker.queue.get(timeout=10)
        assert message.body.decode() == identifier
        await message.ack()
        await broker.queue.delete(if_unused=False, if_empty=True)
    finally:
        await broker.close()


@pytest.mark.live
async def test_real_shared_vlm_vision_stream_schema(request):
    """Отправляет реальное изображение страницы, проверяет streaming/schema модели."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Не указан --vlm")
    import pymupdf

    model = SharedVlmClient(Settings())
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 128, 128), False)
    pixmap.clear_with(255)
    try:
        assert await model.ping()
        prompt = await asyncio.to_thread(
            Path("prompts/extract_specification_v1.txt").read_text, encoding="utf8"
        )
        chunks = [
            part
            async for part in model.stream(
                PreparedPage("", [], [pixmap.tobytes("png")], []),
                prompt,
                {"pdf_page": 1},
            )
        ]
        assert chunks
        result = parse_model_response("".join(chunks))
        assert result.rows == []
    finally:
        await model.close()
