# tests/test_operations.py
"""Проверка безопасной AMQP-диагностики; внешнее соединение здесь явно заменено double."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import operations
from app.core.settings import Settings


@pytest.fixture
def broker_root():
    """Предоставляет только диагностический порт, без реального брокера или секретов."""
    queue = SimpleNamespace(declaration_result=SimpleNamespace(consumer_count=1))
    return SimpleNamespace(
        settings=Settings(_env_file=None),
        broker=SimpleNamespace(
            ping=AsyncMock(return_value=True),
            channel=SimpleNamespace(declare_queue=AsyncMock(return_value=queue)),
            close=AsyncMock(),
        ),
        model=SimpleNamespace(close=AsyncMock()),
    )


async def test_broker_check_requires_actual_consumer(broker_root, capsys):
    """Использует passive declare рабочей очереди, не читая и не публикуя сообщения."""
    assert await operations.broker_check(True, broker_root) == 0
    broker_root.broker.channel.declare_queue.assert_awaited_once_with(
        "specification.extractions", passive=True
    )
    assert "consumers=1" in capsys.readouterr().out
    broker_root.broker.close.assert_awaited_once()
    broker_root.model.close.assert_awaited_once()


async def test_broker_check_fails_without_worker(broker_root, monkeypatch, capsys):
    """Отклоняет очередь без подписанного consumer после конечного ожидания."""
    broker_root.broker.channel.declare_queue.return_value.declaration_result.consumer_count = 0
    clock = iter([0, 100])
    monkeypatch.setattr(operations, "monotonic", lambda: next(clock))
    assert await operations.broker_check(True, broker_root) == 1
    assert "worker" in capsys.readouterr().err


async def test_broker_check_waits_for_worker_start(broker_root, monkeypatch):
    """Не объявляет отказ, если worker подписывается после первого passive declare."""
    channel = broker_root.broker.channel
    ready = channel.declare_queue.return_value
    empty = SimpleNamespace(declaration_result=SimpleNamespace(consumer_count=0))
    channel.declare_queue.side_effect = [empty, ready]
    sleeper = AsyncMock()
    monkeypatch.setattr(operations.asyncio, "sleep", sleeper)
    assert await operations.broker_check(True, broker_root) == 0
    sleeper.assert_awaited_once_with(0.5)


@pytest.mark.parametrize("failed_ping", [True, False])
async def test_broker_error_does_not_disclose_credentials(broker_root, failed_ping, capsys):
    """Соединение/канал не выводят значения из исключения, содержащего тестовый пароль."""
    if failed_ping:
        broker_root.broker.ping.return_value = False
    else:
        broker_root.broker.channel.declare_queue.side_effect = RuntimeError(
            "amqp://u:private-credential@host"
        )
    assert await operations.broker_check(True, broker_root) == 1
    assert "private-credential" not in str(capsys.readouterr())
    broker_root.broker.close.assert_awaited_once()


@pytest.mark.parametrize("operation", ["config", "broker-check"])
def test_cli_invalid_settings_never_print_input(operation, monkeypatch, capsys):
    """Публичные команды скрывают Pydantic input_value при ошибке настройки."""
    monkeypatch.setattr("sys.argv", ["app.operations", operation])

    def invalid_settings():
        """Создаёт настоящий ValidationError с секретным тестовым входом."""
        return Settings(_env_file=None, api_keys="private-credential")

    monkeypatch.setattr(operations, "Settings", invalid_settings)
    assert operations.main() == 2
    assert "private-credential" not in str(capsys.readouterr())


def test_cli_missing_secret_is_redacted(monkeypatch, capsys):
    """Короткий пароль останавливает config безопасным сообщением."""
    settings = Settings(_env_file=None, database_password="short")
    monkeypatch.setattr("sys.argv", ["app.operations", "config"])
    monkeypatch.setattr(operations, "Settings", lambda: settings)
    assert operations.main() == 2
    assert "short" not in str(capsys.readouterr())


async def test_broker_cleanup_and_initialization_errors_are_redacted(
    broker_root, monkeypatch, capsys
):
    """Не выпускает секретные тексты ошибок конструктора и закрытия зависимостей."""
    broker_root.broker.close.side_effect = RuntimeError("private-credential")
    assert await operations.broker_check(True, broker_root) == 0

    def failed_build():
        """Имитирует ошибку до открытия соединения."""
        raise ValueError("private-credential")

    monkeypatch.setattr(operations, "build_container", failed_build)
    assert await operations.broker_check(True) == 1
    assert "private-credential" not in str(capsys.readouterr())
