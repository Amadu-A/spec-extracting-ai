# tests/test_broker_contract.py
"""Offline AMQP adapter contract и durable outbox; это не live проверка RabbitMQ."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, Mock

import pytest
from pamqp.commands import Basic

from app.infrastructure.broker import RabbitPublisher
from app.worker import maintenance
from tests.conftest import uploaded


async def test_amqp_persistent_publish_confirms_qos_and_safe_failure(root, monkeypatch):
    """Проверяет wire-настройки production adapter без утверждения реального broker runtime."""
    channel = Mock(is_closed=False)
    channel.set_qos = AsyncMock()
    channel.declare_queue = AsyncMock(return_value=Mock())
    channel.default_exchange.publish = AsyncMock(return_value=Basic.Ack())
    connection = Mock(is_closed=False, channel=AsyncMock(return_value=channel), close=AsyncMock())
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr("app.infrastructure.broker.aio_pika.connect_robust", connect)
    broker = RabbitPublisher(root.settings)
    assert await broker.ping()
    await broker.publish("00000000-0000-0000-0000-000000000001", 4)
    connection.channel.assert_awaited_once_with(publisher_confirms=True, on_return_raises=True)
    channel.set_qos.assert_awaited_once_with(prefetch_count=1)
    message = channel.default_exchange.publish.await_args.args[0]
    assert message.delivery_mode == 2 and message.body.startswith(b"0000")
    assert message.headers == {"revision": 4}
    assert channel.default_exchange.publish.await_args.kwargs["mandatory"] is True
    for failed in (Basic.Nack(), Basic.Reject(), None):
        channel.default_exchange.publish.return_value = failed
        with pytest.raises(RuntimeError, match="не подтвердил"):
            await broker.publish("job", 4)
    await broker.close()
    connection.close.assert_awaited_once()
    connect.side_effect = ConnectionError
    unavailable = RabbitPublisher(root.settings)
    assert await unavailable.ping() is False
    with pytest.raises(ConnectionError):
        await unavailable.publish("job")


async def test_db_outbox_publishes_without_browser_and_preserves_state(root, pdf_bytes):
    """Worker dispatcher находит committed queued job даже без API process/browser."""
    doc = await uploaded(root, pdf_bytes)
    job = await root.extractions.start("api:alpha", doc.id, "1", "P")
    published = asyncio.Event()

    async def publish(job_id: str, revision: int):
        """Заменяет только AMQP-доставку, не durable DB-outbox."""
        assert job_id == job.id
        assert revision == job.revision
        published.set()

    root.broker.publish = publish
    task = asyncio.create_task(maintenance(root))
    try:
        await asyncio.wait_for(published.wait(), 2)
        assert (await root.repository.get(job.id)).state.value == "queued"
        assert (root.settings.data_directory / "worker.heartbeat").exists()
        assert not root.model.calls
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_connect_retries_after_queue_declaration_cancelled(root, monkeypatch):
    """Прерванный setup не оставляет пригодным channel, у которого ещё нет очереди."""
    channel = Mock(is_closed=False, set_qos=AsyncMock())
    channel.declare_queue = AsyncMock(side_effect=[asyncio.CancelledError, Mock()])
    connection = Mock(is_closed=False, channel=AsyncMock(return_value=channel), close=AsyncMock())
    dial = AsyncMock(return_value=connection)
    monkeypatch.setattr("app.infrastructure.broker.aio_pika.connect_robust", dial)
    broker = RabbitPublisher(root.settings)
    with pytest.raises(asyncio.CancelledError):
        await broker.connect()
    connection.close.assert_awaited_once()
    assert broker.queue is None and broker.channel is None
    await broker.connect()
    assert broker.queue is not None and dial.await_count == 2
