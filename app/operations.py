# app/operations.py
"""Операторские проверки: реальные зависимости, vision/schema и heartbeat worker."""

import argparse
import asyncio
import sys
from time import time

import httpx

from app.application.ports import PreparedPage
from app.bootstrap import build_container
from app.core.settings import Settings
from app.domain.reconcile import parse_model_response


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
    """Исполняет конечные проверки без логирования настроек/секретов."""
    parser = argparse.ArgumentParser(description="Проверки сервиса спецификаций")
    parser.add_argument(
        "operation", choices=("config", "preflight", "vision", "ready", "worker-health")
    )
    arguments = parser.parse_args()
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
