# tests/conftest.py
"""DI fixtures: настоящие storage/PDF/repository, детерминированная модель только в тестах."""

import asyncio
import json
from pathlib import Path

import httpx
import pymupdf
import pytest

from app.application.exports import ExportService
from app.application.processor import ExtractionProcessor
from app.application.service import ExtractionService
from app.application.sessions import SessionService
from app.bootstrap import Container
from app.core.settings import Settings
from app.infrastructure.database import Base, SqlRepository
from app.infrastructure.excel import ExcelExporter
from app.infrastructure.pdf import IsolatedPdfReader
from app.infrastructure.storage import LocalStorage
from app.presentation.http.app import create_app

KEY_A = "test-alpha-key-for-offline-checks-only"
KEY_B = "test-beta-key-for-offline-checks-only"
PASSWORD = "test-ui-password-only"


def pytest_addoption(parser):
    """Явные флаги отделяют реальные зависимости от offline тестов и test doubles."""
    parser.addoption(
        "--postgres", action="store_true", help="Проверять реальную PostgreSQL из Settings"
    )
    parser.addoption(
        "--broker", action="store_true", help="Проверять app-vhost shared RabbitMQ из Settings"
    )
    parser.addoption(
        "--vlm", action="store_true", help="Проверять vision/schema реального shared-vlm"
    )
    parser.addoption(
        "--require-corpus", action="store_true", help="Требовать все четыре локальных PDF/JSON"
    )


def pytest_collection_modifyitems(config, items):
    """Не позволяет объявить полный corpus-прогон успешным без исходных документов."""
    if config.getoption("--require-corpus"):
        from scripts.evaluate import find_pairs

        if len(find_pairs(Path("examples"))) != 4:
            raise pytest.UsageError("--require-corpus: нужны все четыре PDF/JSON в examples/")


class DeterministicModel:
    """Тестовый double; production composition root никогда его не использует."""

    def __init__(self):
        """Позволяет сценариям задать ошибки/задержку/ответ без подмены алгоритма."""
        self.responses = []
        self.calls = []
        self.delay = 0
        self.available = True

    async def stream(self, page, prompt: str, context: dict):
        """Возвращает воспроизводимые chunks с тем же async-контрактом, что shared-vlm."""
        self.calls.append({"prompt": prompt, "context": context, "image_count": len(page.images)})
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
        else:
            response = json.dumps(
                {
                    "rows": [
                        {
                            "row_id": "table1/row1",
                            "designation": "",
                            "manufacturer": "",
                            "caption": f"Valve page {context['pdf_page']}",
                            "units": "шт",
                            "quantity": "2,5",
                            "continuation": False,
                            "continues_next": False,
                            "verified": True,
                        }
                    ],
                    "group_context": [],
                    "warnings": [],
                    "completeness": "complete",
                },
                ensure_ascii=False,
            )
        for offset in range(0, len(response), 60):
            await asyncio.sleep(self.delay)
            yield response[offset : offset + 60]

    async def ping(self) -> bool:
        """Возвращает управляемую готовность исключительно тестового адаптера."""
        return self.available

    async def close(self) -> None:
        """Double не владеет HTTP ресурсами."""


class FakeBroker:
    """Изолирует offline тесты от credentials shared RabbitMQ."""

    async def ping(self) -> bool:
        """Проверки настоящего AMQP помечаются отдельно live."""
        return True

    async def close(self) -> None:
        """Не закрывает никакие реальные соединения."""


@pytest.fixture
def root(tmp_path) -> Container:
    """Собирает production use-case с SQLite/PDF/storage и явно тестовыми зависимостями."""
    settings = Settings(
        _env_file=None,
        data_directory=tmp_path / "files",
        ui_password=PASSWORD,
        api_keys={"alpha": KEY_A, "beta": KEY_B},
        vlm_attempts=1,
        heartbeat_seconds=1,
        rate_limit_per_minute=10000,
    )
    repository = SqlRepository("sqlite:///" + str(tmp_path / "test.sqlite"))
    Base.metadata.create_all(repository.engine)
    storage, pdf, model = (
        LocalStorage(settings.data_directory),
        IsolatedPdfReader(settings),
        DeterministicModel(),
    )
    default = Path("prompts/extract_specification_v1.txt").read_text(encoding="utf-8")
    extractions = ExtractionService(repository, storage, pdf, settings, default)
    return Container(
        settings,
        repository,
        storage,
        pdf,
        model,
        FakeBroker(),
        extractions,
        SessionService(repository, settings, default),
        ExtractionProcessor(repository, storage, pdf, model, settings),
        ExportService(extractions, ExcelExporter()),
    )


@pytest.fixture
async def client(root):
    """Реальный ASGI transport, без поднятия сетевого HTTP-процесса."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(root)), base_url="http://test"
    ) as connection:
        yield connection


@pytest.fixture
def pdf_bytes() -> bytes:
    """Создаёт три физически разные PDF-страницы для unit/integration сценариев."""
    with pymupdf.open() as document:
        for number in range(1, 4):
            page = document.new_page()
            page.insert_text((60, 60), f"Specification page {number}: valve DN100, quantity 2.5")
        return document.tobytes()


async def uploaded(root: Container, data: bytes, owner: str = "api:alpha"):
    """Передаёт тестовый PDF через тот же upload use-case, что HTTP."""

    async def chunks():
        """Имитирует только транспортные chunks, сохраняя настоящий PDF parser."""
        yield data

    return await root.extractions.upload(owner, "test.pdf", "application/pdf", chunks())
