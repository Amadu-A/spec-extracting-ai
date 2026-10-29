# tests/test_logging.py
"""Timing decorator сохраняет metadata и логирует error при sync/async отказе/отмене."""

import asyncio
import logging

import pytest

from app.core.observability import JsonFormatter, timed


@pytest.mark.parametrize("fail", [False, True])
def test_sync_timing(caplog, fail):
    """Sync операция пишет ровно один duration/status и не теряет имя функции."""

    @timed("sync_test")
    def operation():
        """Воспроизводит успешную и ошибочную ветви без внешнего I/O."""
        if fail:
            raise ValueError("failure")
        return 4

    with caplog.at_level(logging.INFO):
        if fail:
            with pytest.raises(ValueError):
                operation()
        else:
            assert operation() == 4
    record = caplog.records[-1]
    assert record.duration_ms >= 0 and record.status == ("error" if fail else "success")
    assert operation.__name__ == "operation" and len(caplog.records) == 1


def test_dependency_log_cannot_expose_connection_password():
    """Текст стороннего клиента не попадает в stdout с возможным DSN/password."""
    record = logging.LogRecord(
        "aio_pika.connection",
        logging.ERROR,
        __file__,
        1,
        "Failed amqp://user:private-password@rabbitmq/vhost",
        (),
        None,
    )
    output = JsonFormatter().format(record)
    assert "private-password" not in output and "dependency_log" in output


@pytest.mark.parametrize("mode", ["success", "error", "cancel"])
async def test_async_timing(caplog, mode):
    """Async cancellation остаётся CancelledError и никогда не записывается как success."""

    @timed("async_test")
    async def operation():
        """Моделирует независимые исходы coroutine."""
        if mode == "error":
            raise ValueError
        if mode == "cancel":
            raise asyncio.CancelledError
        return 4

    with caplog.at_level(logging.INFO):
        if mode == "success":
            assert await operation() == 4
        else:
            with pytest.raises(ValueError if mode == "error" else asyncio.CancelledError):
                await operation()
    record = caplog.records[-1]
    assert record.duration_ms >= 0 and record.status == (
        "success" if mode == "success" else "error"
    )
