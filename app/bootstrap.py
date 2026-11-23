# app/bootstrap.py
"""Единственная точка сборки production-адаптеров и use-case; поддерживает DI тестов."""

from dataclasses import dataclass
from pathlib import Path

from app.application.exports import ExportService
from app.application.invoices.processor import InvoiceProcessor, ProfileProcessor
from app.application.invoices.service import InvoiceService
from app.application.processor import ExtractionProcessor
from app.application.service import ExtractionService
from app.application.sessions import SessionService
from app.core.settings import Settings
from app.infrastructure.broker import RabbitPublisher
from app.infrastructure.database import SqlRepository
from app.infrastructure.excel import ExcelExporter
from app.infrastructure.invoices.excel import InvoiceExcelExporter
from app.infrastructure.invoices.pages import InvoicePages
from app.infrastructure.invoices.vlm import InvoiceVlmClient
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
    invoices: InvoiceService | None = None
    invoice_processor: InvoiceProcessor | None = None
    jobs: ProfileProcessor | None = None


def configure_invoices(root: Container, model=None) -> Container:
    """Подключает новый профиль к общим адаптерам; тесты передают отдельный model double."""
    resources = Path(__file__).resolve().parent / "resources/prompts/invoices"
    pages = InvoicePages(root.pdf)
    root.invoices = InvoiceService(
        root.repository,
        root.storage,
        pages,
        root.settings,
        (resources / "page_extract.txt").read_text(encoding="utf8"),
        (resources / "document_reconcile.txt").read_text(encoding="utf8"),
        InvoiceExcelExporter(),
    )
    root.invoice_processor = InvoiceProcessor(
        root.repository,
        root.storage,
        pages,
        model or InvoiceVlmClient(root.settings, root.model.client),
        root.settings,
    )
    root.jobs = ProfileProcessor(root.repository, root.processor, root.invoice_processor)
    return root


def build_container(settings: Settings | None = None) -> Container:
    """Читает Settings и версионированный prompt, не создаёт таблицы/общие сервисы."""
    settings = settings or Settings()
    settings.require_secrets()
    repository = SqlRepository(settings.database_url)
    storage, pdf = LocalStorage(settings.data_directory), IsolatedPdfReader(settings)
    model, broker = SharedVlmClient(settings), RabbitPublisher(settings)
    default = (
        Path(__file__).resolve().parent.parent / "prompts/extract_specification_v2.txt"
    ).read_text(encoding="utf-8")
    extractions = ExtractionService(repository, storage, pdf, settings, default)
    return configure_invoices(
        Container(
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
    )
