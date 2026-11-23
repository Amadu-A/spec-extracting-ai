# app/domain/invoices/models.py
"""Строгий invoice_extraction.v1 и внутренние постраничные контрольные точки."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, get_args

from pydantic import ConfigDict, Field, StrictStr, field_validator, model_validator

from app.domain.models import Contract, Job

Text = Annotated[StrictStr, Field(max_length=20000)]
Code = Annotated[StrictStr, Field(max_length=1000)]
Number = Annotated[StrictStr, Field(pattern=r"^-?\d{1,24}(?:\.\d{1,12})?$", max_length=38)]
Pages = Annotated[list[Annotated[int, Field(ge=1)]], Field(max_length=500)]


def decimal_text(value):
    """Убирает только разделители тысяч/запятую; float и бесконечность запрещены."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Число должно быть десятичной строкой")
    value = value.replace("\u00a0", "").replace("\u202f", "").replace(" ", "").replace(",", ".")
    if value.endswith("."):
        value = value[:-1]
    try:
        if not Decimal(value).is_finite():
            raise ValueError("Неограниченное число")
    except InvalidOperation as exc:
        raise ValueError("Некорректное десятичное число") from exc
    return value


class InvoiceContract(Contract):
    """Structured output требует явный null вместо пропуска финансовых полей."""

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    @model_validator(mode="before")
    @classmethod
    def empty_optional_fields(cls, value):
        """Пустая исходная ячейка nullable-поля представляется null, а не пустой строкой."""
        if not isinstance(value, dict):
            return value
        return {
            key: None
            if isinstance(item, str)
            and not item.strip()
            and key in cls.model_fields
            and type(None) in get_args(cls.model_fields[key].annotation)
            else item
            for key, item in value.items()
        }


class Party(InvoiceContract):
    """Реквизиты стороны из источника; банковские данные сюда не подставляются."""

    name: Text | None = None
    inn: Code | None = None
    kpp: Code | None = None


class InvoiceItem(InvoiceContract):
    """Позиция сохраняет опубликованные суммы и происхождение без скрытых вычислений."""

    row_number: int = Field(ge=1)
    caption: Text = Field(min_length=1)
    brand: Code | None = None
    manufacturer: Code | None = None
    article: Code | None = None
    article_candidate: Code | None = None
    supplier_code: Code | None = None
    product_reference_raw: Text | None = None
    row_kind: Literal["product", "service", "final_payment", "advance_payment", "other"] = "product"
    units: Code | None = None
    quantity: Number | None = None
    unit_price_without_vat: Number | None = None
    unit_price_with_vat: Number | None = None
    line_total_without_vat: Number | None = None
    vat_rate: Number | None = None
    vat_amount: Number | None = None
    line_total_with_vat: Number | None = None
    source_pages: Pages = Field(default_factory=list)
    vat_rate_source: Literal["row", "document_total"] | None = None
    article_candidate_source: Text | None = None
    technical_parameters_pages: Pages = Field(default_factory=list)
    requires_manual_review: bool = False
    marked_with_asterisk: bool = False
    availability_note: Text | None = None
    order_multiplicity: Number | None = None
    field_origin: dict[str, Literal["source", "derived", "document_total"]] = Field(
        default_factory=dict
    )
    derived_fields: list[Code] = Field(default_factory=list)

    @field_validator(
        "quantity",
        "unit_price_without_vat",
        "unit_price_with_vat",
        "line_total_without_vat",
        "vat_rate",
        "vat_amount",
        "line_total_with_vat",
        "order_multiplicity",
        mode="before",
    )
    @classmethod
    def numbers(cls, value):
        """Нормализует запись числа, не округляя и не меняя исходную величину."""
        return decimal_text(value)


class Adjustment(InvoiceContract):
    """Явный компонент вне строк; уже учтённая скидка повторно не вычитается."""

    kind: Literal["discount", "delivery", "advance", "other"]
    without_vat: Number | None = None
    vat: Number | None = None
    with_vat: Number | None = None
    included_in_line_totals: bool


class Totals(InvoiceContract):
    """Итоги документа независимы от заполнения сумм отдельных строк."""

    without_vat: Number | None = None
    vat: Number | None = None
    with_vat: Number | None = None
    adjustments: list[Adjustment] = Field(default_factory=list, max_length=100)

    @field_validator("without_vat", "vat", "with_vat", mode="before")
    @classmethod
    def numbers(cls, value):
        """Принимает только конечные десятичные строки."""
        return decimal_text(value)


class Check(InvoiceContract):
    """Измеренное сравнение либо честная невозможность его выполнить."""

    check: str
    status: Literal["passed", "failed", "not_checkable"]
    calculated: str | None = None
    document_value: str | None = None
    difference: str | None = None
    tolerance: str | None = None
    row_number: int | None = None


class Validation(InvoiceContract):
    """Агрегат проверок backend; модель не определяет арифметический статус."""

    status: Literal["passed", "partially_checked", "failed", "not_checkable"] = "not_checkable"
    checks: list[Check] = Field(default_factory=list)
    warnings: list[Text] = Field(default_factory=list)


class InvoiceDocument(InvoiceContract):
    """Отдельный закупочный документ внутри одного исходного файла."""

    document_type: Literal["invoice", "commercial_offer", "specification", "unknown"] = "unknown"
    number: Code | None = None
    date: Code | None = None
    supplier: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party)
    currency: Code | None = None
    items: list[InvoiceItem] = Field(default_factory=list, max_length=20000)
    totals: Totals = Field(default_factory=Totals)
    validation: Validation = Field(default_factory=Validation)
    source_pages: Pages = Field(default_factory=list)
    attached_pages: Pages = Field(default_factory=list)
    related_commercial_offer_number: Code | None = None
    payment_terms: Text | None = None
    contract_reference: Text | None = None
    specification_reference: Text | None = None
    notes: list[Text] = Field(default_factory=list, max_length=200)

    @field_validator("currency", mode="before")
    @classmethod
    def currency_code(cls, value):
        """Переводит явное русское обозначение рубля в код RUB, не задавая валюту по умолчанию."""
        if isinstance(value, str):
            normalized = "".join(value.casefold().split())
            if normalized in {"руб", "руб.", "рубль", "рублей", "российскийрубль", "rub"}:
                return "RUB"
            if len(value) == 3 and value.isascii() and value.isalpha():
                return value.upper()
        return value

    @field_validator("date", mode="before")
    @classmethod
    def iso_date(cls, value):
        """Дата должна быть реальной календарной датой ISO, иначе модель повторяется."""
        if (
            isinstance(value, str)
            and len(value) == 10
            and value[2:3] in (".", "/")
            and value[5:6] == value[2:3]
        ):
            value = datetime.strptime(value, f"%d{value[2]}%m{value[2]}%Y").date().isoformat()
        if value is not None and date.fromisoformat(value).isoformat() != value:
            raise ValueError("Ожидается дата ISO YYYY-MM-DD")
        return value


class InvoiceFile(InvoiceContract):
    """Публичный источник; имя, хэш и число страниц задаёт только backend."""

    file_index: int = Field(ge=0)
    source_file: Code
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_count: int = Field(ge=1)
    documents: list[InvoiceDocument] = Field(default_factory=list)


class Summary(InvoiceContract):
    """Счётчики без объединения денежных итогов разных контрагентов."""

    file_count: int
    document_count: int
    item_count: int


class InvoiceExport(InvoiceContract):
    """Одна схема для одиночного файла и смешанного пакета."""

    schema_version: Literal["invoice_extraction.v1"] = "invoice_extraction.v1"
    files: list[InvoiceFile]
    summary: Summary


class ExtractedItem(InvoiceItem):
    """Компактная схема модели: provenance назначает сервер, вычисляемые поля не запрашиваются."""

    source_pages: Pages = Field(default_factory=list, exclude=True)
    field_origin: dict = Field(default_factory=dict, exclude=True)
    derived_fields: list[Code] = Field(default_factory=list, exclude=True)
    technical_parameters_pages: Pages = Field(default_factory=list, exclude=True)
    requires_manual_review: bool = Field(False, exclude=True)
    marked_with_asterisk: bool = Field(False, exclude=True)
    availability_note: Text | None = Field(None, exclude=True)
    order_multiplicity: Number | None = Field(None, exclude=True)
    article_candidate_source: Text | None = Field(None, exclude=True)
    article_source: Literal[
        "column", "explicit_marker", "mixed_column", "caption_candidate", "none"
    ] = "none"


class InvoiceRow(InvoiceContract):
    """Физический фрагмент; одинаковые товары в разных строках сохраняются."""

    row_id: Code = Field(min_length=1)
    item: ExtractedItem
    continuation: bool = False
    continues_next: bool = False


class Fragment(InvoiceDocument):
    """Ответ одной страницы с явным признаком начала нового документа."""

    starts_document: bool = False
    article_header: Code = ""
    document_vat_rate: Number | None = None
    line_columns: (
        list[Literal["line_total_without_vat", "vat_amount", "line_total_with_vat"]] | None
    ) = None
    items: list[InvoiceItem] = Field(default_factory=list, exclude=True)
    validation: Validation = Field(default_factory=Validation, exclude=True)
    source_pages: Pages = Field(default_factory=list, exclude=True)
    attached_pages: Pages = Field(default_factory=list, exclude=True)
    rows: list[InvoiceRow] = Field(default_factory=list, max_length=2000)


class InvoicePage(InvoiceContract):
    """Постраничный structured output; неполный лист нельзя финализировать успешно."""

    role: Literal["invoice", "commercial_offer", "specification", "attachment", "irrelevant"]
    documents: list[Fragment] = Field(default_factory=list, max_length=30)
    completeness: Literal["complete", "uncertain"]
    warnings: list[Annotated[StrictStr, Field(max_length=600)]] = Field(
        default_factory=list, max_length=200
    )

    @model_validator(mode="after")
    def physical_rows(self):
        """Коллизия идентификатора требует повторения, а не удаления позиции."""
        ids = [row.row_id for doc in self.documents for row in doc.rows]
        if len(ids) != len(set(ids)) or any(doc.items for doc in self.documents):
            raise ValueError("Позиции должны быть только в rows с уникальными row_id")
        return self


class FinancialRow(InvoiceContract):
    """Повторное чтение только числовых граф сложной платёжной строки, без вычислений."""

    row_number: int = Field(ge=1)
    units: Code | None = None
    quantity: Number | None = None
    unit_price_without_vat: Number | None = None
    unit_price_with_vat: Number | None = None
    line_total_without_vat: Number | None = None
    vat_rate: Number | None = None
    vat_amount: Number | None = None
    line_total_with_vat: Number | None = None

    @field_validator(
        "quantity",
        "unit_price_without_vat",
        "unit_price_with_vat",
        "line_total_without_vat",
        "vat_rate",
        "vat_amount",
        "line_total_with_vat",
        mode="before",
    )
    @classmethod
    def numbers(cls, value):
        """Нормализует только запись исходного числа."""
        return decimal_text(value)


class FinancialReview(InvoiceContract):
    """Компактная схема уточнения цен/сумм при неоднозначной налоговой трактовке скана."""

    document_number: Code | None = None
    document_vat_rate: Number | None = None
    line_columns: list[Literal["line_total_without_vat", "vat_amount", "line_total_with_vat"]]
    rows: list[FinancialRow] = Field(max_length=2000)


class Source(Contract):
    """Приватная связь пакета с общим UUID-хранилищем документов."""

    document_id: str
    filename: str
    sha256: str
    page_count: int
    selected_pages: list[int]
    media_type: str


class Checkpoint(Contract):
    """Соответствие плоского lifecycle листа файлу и committed ответу модели."""

    file_index: int
    pdf_page: int
    extraction: InvoicePage | None = None
    document_vat_rates: list[Number] = Field(default_factory=list)
    included_discount_amounts: list[Number] = Field(default_factory=list)
    native_tables: list[dict] = Field(default_factory=list)
    retry_count: int = 0
    financial_review: FinancialReview | None = None


class InvoiceJob(Job):
    """Использует существующие jobs/leases/events, не меняя legacy snapshot."""

    job_type: Literal["invoice"] = "invoice"
    sources: list[Source]
    checkpoints: list[Checkpoint]
    current_file_index: int | None = None
    reconcile_prompt: str
    result: InvoiceExport | None = None
    diagnostics: list[dict] = Field(default_factory=list)

    def emit(self, event_type: str, **data) -> None:
        """Каждое новое событие несёт независимые file/page/document координаты."""
        super().emit(
            event_type,
            **{
                "file_index": self.current_file_index,
                "pdf_page": self.current_pdf_page,
                "document_index": None,
                **data,
            },
        )
