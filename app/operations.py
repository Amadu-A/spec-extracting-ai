# app/operations.py
"""Операторские проверки и закрытый протокол настроек RabbitMQ для scripts/up.sh."""

import argparse
import asyncio
import re
import sys
from time import time

import httpx
from pydantic import ValidationError
from pydantic_settings import SettingsError

from app.application.ports import PreparedPage
from app.bootstrap import build_container
from app.core.settings import Settings
from app.domain.reconcile import parse_model_response


def broker_bootstrap_values(settings: Settings) -> tuple[str, str, str, str, str]:
    """Проверяет закрытый протокол up.sh; четвёртая строка содержит секрет.

    Bash захватывает stdout в память и передаёт пароль через stdin существующему
    RabbitMQ. Значения dotenv не исполняются и не попадают в журнал запуска.
    """
    settings.require_secrets()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", settings.rabbitmq_container):
        raise ValueError("RABBITMQ_CONTAINER: укажите имя существующего контейнера RabbitMQ")
    for name, value in (
        ("RABBITMQ_USER", settings.rabbitmq_user),
        ("RABBITMQ_VHOST", settings.rabbitmq_vhost),
    ):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
            raise ValueError(f"{name}: требуется отдельное имя из букв, цифр, точки, дефиса или _")
    if settings.rabbitmq_user.lower() in {"guest", "admin", "administrator", "shared_admin"}:
        raise ValueError("RabbitMQ: нужен отдельный пользователь приложения без служебных прав")
    password = settings.rabbitmq_password.get_secret_value()
    if len(password.encode("utf-8")) > 1024 or any(char in password for char in "\r\n\0"):
        raise ValueError("RABBITMQ_PASSWORD: требуется однострочный секрет длиной до 1024 байт")
    return (
        settings.rabbitmq_container,
        settings.rabbitmq_user,
        settings.rabbitmq_vhost,
        password,
        str(settings.rabbitmq_cli_timeout_seconds),
    )


def broker_config() -> int:
    """Передаёт секретный протокол только up.sh; ошибки не раскрывают значения настроек."""
    try:
        values = broker_bootstrap_values(Settings())
    except (ValidationError, SettingsError):
        print("Некорректные настройки: проверьте .env.example, .env и окружение", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    sys.stdout.write("\n".join(values) + "\n")
    return 0


async def preflight(vision: bool) -> int:
    """Не меняет shared config; проверяет существующие credentials, alias и JSON modality."""
    root = build_container()
    try:
        checks = await asyncio.gather(root.repository.ping(), root.broker.ping(), root.model.ping())
        for name, result in zip(
            ("PostgreSQL", "RabbitMQ app-vhost", "shared-vlm alias"), checks, strict=True
        ):
            print(f"{name}: {'OK' if result else 'НЕДОСТУПЕН'}", flush=True)
        if not all(checks):
            return 1
        if vision:
            # PNG строится локально; shared получает картинку, а не только text prompt.
            import pymupdf

            pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 128, 128), False)
            pixmap.clear_with(255)
            page = PreparedPage(
                text="Пустой белый лист, товаров нет",
                blocks=[],
                images=[pixmap.tobytes("png")],
                warnings=[],
            )
            answer = ""
            async for delta in root.model.stream(
                page, "Верни пустую страницу без товарных строк", {"pdf_page": 1}
            ):
                answer += delta
            parsed = parse_model_response(answer)
            if parsed.rows:
                print("Vision/schema smoke: модель выдумала строки на пустом листе", flush=True)
                return 1
            print("Vision + streaming + JSON schema: OK", flush=True)
        return 0
    finally:
        await root.broker.close()
        await root.model.close()


def main() -> int:
    """Исполняет проверки; broker-config отдаёт секретный stdout только родительскому up.sh."""
    parser = argparse.ArgumentParser(description="Проверки сервиса спецификаций")
    parser.add_argument(
        "operation",
        choices=("config", "broker-config", "preflight", "vision", "ready", "worker-health"),
    )
    arguments = parser.parse_args()
    if arguments.operation == "broker-config":
        return broker_config()
    settings = Settings()
    if arguments.operation == "config":
        settings.require_secrets()
        print("Settings/secrets: OK")
        return 0
    if arguments.operation in ("preflight", "vision"):
        return asyncio.run(preflight(arguments.operation == "vision"))
    if arguments.operation == "worker-health":
        try:
            stamp = float(
                (settings.data_directory / "worker.heartbeat").read_text(encoding="ascii")
            )
            return 0 if time() - stamp < 45 else 1
        except (OSError, ValueError):
            return 1
    try:
        response = httpx.get(
            "http://127.0.0.1:8083/api/v1/health/ready", timeout=12, trust_env=False
        )
        return 0 if response.status_code == 200 else 1
    except httpx.HTTPError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
