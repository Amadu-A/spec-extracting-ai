# app/bootstrap.py
"""Единственная точка сборки production-адаптеров и use-case; поддерживает DI тестов."""

from dataclasses import dataclass
from pathlib import Path

from app.application.exports import ExportService
from app.application.processor import ExtractionProcessor
from app.application.service import ExtractionService
from app.application.sessions import SessionService
from app.core.settings import Settings
from app.infrastructure.broker import RabbitPublisher
from app.infrastructure.database import SqlRepository
from app.infrastructure.excel import ExcelExporter
from app.infrastructure.pdf import IsolatedPdfReader
from app.infrastructure.storage import LocalStorage
from app.infrastructure.vlm import SharedVlmClient


@dataclass
class Container:
    """Явно передаёт готовые use-case транспорту и worker без service locator в domain."""

    settings: Settings
    repository: SqlRepository
    storage: LocalStorage
    pdf: IsolatedPdfReader
    model: SharedVlmClient
    broker: RabbitPublisher
    extractions: ExtractionService
    sessions: SessionService
    processor: ExtractionProcessor
    exports: ExportService


def build_container(settings: Settings | None = None) -> Container:
    """Читает Settings и версионированный prompt, не создаёт таблицы/общие сервисы."""
    settings = settings or Settings()
    settings.require_secrets()
    repository = SqlRepository(settings.database_url)
    storage, pdf = LocalStorage(settings.data_directory), IsolatedPdfReader(settings)
    model, broker = SharedVlmClient(settings), RabbitPublisher(settings)
    default = (
        Path(__file__).resolve().parent.parent / "prompts/extract_specification_v1.txt"
    ).read_text(encoding="utf-8")
    extractions = ExtractionService(repository, storage, pdf, settings, default)
    return Container(
        settings,
        repository,
        storage,
        pdf,
        model,
        broker,
        extractions,
        SessionService(repository, settings, default),
        ExtractionProcessor(repository, storage, pdf, model, settings),
        ExportService(extractions, ExcelExporter()),
    )
