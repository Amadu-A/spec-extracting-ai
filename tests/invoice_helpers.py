# tests/invoice_helpers.py
"""Только синтетические обезличенные данные; не имитируют live-извлечение корпуса."""

import asyncio

from app.domain.invoices.models import (
    ExtractedItem,
    Fragment,
    InvoiceDocument,
    InvoiceItem,
    InvoicePage,
    InvoiceRow,
    Totals,
)


def synthetic_document(**updates):
    """Даёт финансово согласованный документ с повторимым синтетическим товаром."""
    item = InvoiceItem(
        row_number=1,
        caption="Valve 001-A",
        article="001-A",
        units="шт",
        quantity="2",
        unit_price_without_vat="10.00",
        line_total_without_vat="20.00",
        vat_rate="20",
        vat_amount="4.00",
        line_total_with_vat="24.00",
        source_pages=[1],
    )
    document = InvoiceDocument(
        document_type="invoice",
        number="0001/26",
        date="2026-11-20",
        currency="RUB",
        items=[item],
        totals=Totals(without_vat="20.00", vat="4.00", with_vat="24.00"),
        source_pages=[1],
    )
    return document.model_copy(update=updates, deep=True)


def synthetic_page(page: int = 1):
    """Первая страница содержит таблицу; следующие — приложения без товаров."""
    if page != 1:
        return InvoicePage(role="attachment", completeness="complete")
    document = synthetic_document()
    fragment = Fragment.model_validate(document.model_dump())
    fragment.items = []
    fragment.article_header = "Артикул"
    fragment.line_columns = ["line_total_without_vat", "vat_amount", "line_total_with_vat"]
    fragment.rows = [
        InvoiceRow(
            row_id="table1/row1", item=ExtractedItem.model_validate(document.items[0].model_dump())
        )
    ]
    return InvoicePage(role="invoice", completeness="complete", documents=[fragment])


class InvoiceModelDouble:
    """Меняет только внешний inference, сохраняя production parser/repository/processor."""

    def __init__(self):
        """Хранит вызовы для проверок snapshots и восстановления."""
        self.calls = []
        self.responses = {}
        self.delay = 0

    async def stream(self, page, prompt, context):
        """Отдаёт проверяемые chunks; исключения нужны для честных failure-тестов."""
        self.calls.append(dict(prompt=prompt, context=context))
        response = self.responses.get(
            (context["file_index"], context["pdf_page"], context.get("operation")),
            self.responses.get(
                (context["file_index"], context["pdf_page"]), synthetic_page(context["pdf_page"])
            ),
        )
        if isinstance(response, Exception):
            raise response
        raw = response if isinstance(response, str) else response.model_dump_json()
        for offset in range(0, len(raw), 200):
            await asyncio.sleep(self.delay)
            yield raw[offset : offset + 200]
