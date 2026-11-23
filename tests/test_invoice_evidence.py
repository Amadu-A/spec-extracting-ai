# tests/test_invoice_evidence.py
"""Реальная геометрия синтетического PDF и регрессии происхождения финансов/кодов."""

from io import BytesIO

import pymupdf
from openpyxl import load_workbook

from app.domain.invoices.evidence import (
    attachment_heading,
    cell_text,
    document_heading,
    included_discounts,
    mixed_code,
    source_caption,
)
from app.domain.invoices.models import (
    Adjustment,
    FinancialReview,
    FinancialRow,
    Fragment,
    InvoiceExport,
    InvoiceFile,
    InvoiceItem,
    Party,
    Summary,
)
from app.domain.invoices.reconcile import explicit_vat_rates, merge_fields
from app.infrastructure.invoices.excel import build
from tests.invoice_helpers import synthetic_document, synthetic_page
from tests.test_invoices import BASE, HEADERS
from tests.test_live_invoices import caption_key, verify_enrichments


def financial_pdf(rows=1, quantity="3"):
    """Рисует настоящие границы и текст ячеек без внешних шрифтов/реальных реквизитов."""
    headers = [
        "№",
        "Код",
        "Наименование товара",
        "Артикул",
        "Ед.",
        "Кол-во",
        "Цена без НДС",
        "Сумма без НДС",
        "Сумма НДС",
        "Всего с НДС",
    ]
    widths = [30, 55, 180, 70, 45, 50, 80, 80, 80, 80]
    with pymupdf.open() as document:
        page = document.new_page(width=850, height=500)
        for row in range(rows + 1):
            values = (
                headers
                if row == 0
                else [
                    str(row),
                    "00007",
                    "Valve 001-A BRAND",
                    "00a-007",
                    "шт",
                    quantity,
                    "10.00",
                    "30.00",
                    "6.00",
                    "36.00",
                ]
            )
            x = 30
            for width, value in zip(widths, values, strict=True):
                box = pymupdf.Rect(x, 60 + row * 60, x + width, 120 + row * 60)
                page.draw_rect(box, width=0.6)
                page.insert_htmlbox(
                    box + (3, 3, -3, -3), f'<span style="font-size:10px">{value}</span>'
                )
                x += width
        return document.tobytes()


def test_source_words_codes_and_header_roles():
    """Устраняет только очевидные переносы, не теряя дефисы, нули и полный бренд."""
    assert cell_text("screw-mm-4\n.2x19") == "screw-mm-4.2x19"
    assert cell_text("DIN-М6x\n30") == "DIN-М6x30"
    assert cell_text("MVA20-1-00\n3-C") == "MVA20-1-00 3-C"
    assert source_caption("оцинкован ная BRAND", "оцинкованная BRAND") == "оцинкованная BRAND"
    assert source_caption("Valve 001-A BRAND", "Valve 001-A") == "Valve 001-A BRAND"
    assert (
        mixed_code(InvoiceItem(row_number=1, caption="x", product_reference_raw="3M", brand="3M"))
        is None
    )
    assert (
        mixed_code(InvoiceItem(row_number=1, caption="x", product_reference_raw="ahdw-ew"))
        == "ahdw-ew"
    )
    assert explicit_vat_rates("НДС, 22%: 100.00") == ["22"]
    assert explicit_vat_rates("Дата 2026-01-01. Без НДС") == []
    assert not attachment_heading("Счёт №0001\nОплата по договору поставки")
    assert attachment_heading("Соглашение на поставку заказной продукции №0001")
    assert document_heading("ТКП №005\nСпецификация №0013 от 20 января") == dict(
        document_type="specification", number="0013", related_commercial_offer_number="005"
    )
    assert caption_key("Кабель МКЭШ 0.7 5") == caption_key("Кабель МКЭШ 0.75")
    assert caption_key("Кабель ABC-01") != caption_key("Кабель ABC/01")


async def test_native_pdf_columns_preserve_values_and_disclose_model_conflict(
    invoice_root, invoice_client
):
    """Источник, а не ошибочная модель, задаёт код/числа; итог документа не подгоняется."""
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files={"files": ("synthetic.pdf", financial_pdf(), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.result is not None, job.error_code
    item = job.result.files[0].documents[0].items[0]
    assert (
        item.quantity,
        item.line_total_without_vat,
        item.vat_amount,
        item.line_total_with_vat,
    ) == ("3", "30.00", "6.00", "36.00")
    assert (
        item.caption == "Valve 001-A BRAND"
        and item.article == "00a-007"
        and item.supplier_code == "00007"
    )
    assert job.result.files[0].documents[0].totals.with_vat == "24.00"
    assert job.result.files[0].documents[0].validation.status == "failed"
    assert any("противоречит ячейке PDF" in warning["message"] for warning in job.diagnostics)


async def test_missing_native_row_blocks_complete_export(invoice_root, invoice_client):
    """Нельзя завершить пакет, когда модель потеряла физическую строку таблицы."""
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files={"files": ("synthetic.pdf", financial_pdf(rows=2), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.error_code == "invoice_row_count_mismatch"
    assert (
        await invoice_client.get(f"{BASE}/{identifier}/result.json", headers=HEADERS)
    ).status_code == 409


async def test_payment_review_is_conditional_and_keeps_null_quantity(
    invoice_root, invoice_client, pdf_bytes
):
    """Дополнительная VLM-схема применяется только к неоднозначной платёжной строке."""
    model = invoice_root.invoice_processor.model
    page = synthetic_page()
    item = page.documents[0].rows[0].item
    item.row_kind, item.quantity, item.units = "final_payment", None, None
    model.responses[(0, 1)] = page
    model.responses[(0, 1, "financial_review")] = FinancialReview(
        document_number="0001/26",
        document_vat_rate="20",
        line_columns=["line_total_without_vat"],
        rows=[
            FinancialRow(
                row_number=1, unit_price_without_vat="20.00", line_total_without_vat="20.00"
            )
        ],
    )
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files={"files": ("payment.pdf", pdf_bytes, "application/pdf")},
        data={"page_selection": '{"0":"1"}'},
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.result is not None, job.error_code
    item = job.result.files[0].documents[0].items[0]
    assert item.quantity is None and item.units is None and item.line_total_with_vat is None
    assert item.unit_price_without_vat == "20.00" and item.vat_rate_source == "document_total"
    assert len(model.calls) == 2 and job.checkpoints[0].financial_review is not None


def test_excel_keeps_long_decimal_as_exact_text():
    """За пределами точности Excel исходные цифры сохраняются без преобразования в float."""
    document = synthetic_document()
    document.totals.without_vat = "1234567890123456.78"
    result = InvoiceExport(
        files=[
            InvoiceFile(
                file_index=0,
                source_file="safe.pdf",
                source_sha256="0" * 64,
                page_count=1,
                documents=[document],
            )
        ],
        summary=Summary(file_count=1, document_count=1, item_count=1),
    )
    book = load_workbook(BytesIO(build(result, [])))
    assert book["Документы"]["N2"].value == "1234567890123456.78"
    assert book["Документы"]["N2"].data_type == "s"


def test_attachment_cannot_mix_parties_with_different_inn():
    """Обнаруженный конфликт ИНН не дополняет исходную сторону чужим КПП."""
    document = synthetic_document()
    document.buyer = Party(name="First buyer", inn="1111111111")
    fragment = Fragment(buyer=Party(name="Different buyer", inn="2222222222", kpp="222222222"))
    warnings = []
    merge_fields(document, fragment, warnings)
    assert document.buyer.inn == "1111111111" and document.buyer.kpp is None
    assert any("реквизиты разных сторон не объединены" in warning for warning in warnings)
    # Приложения передают реквизиты напрямую, без обёртки InvoiceDocument.
    merge_fields(document.buyer, fragment.buyer, warnings)
    assert document.buyer.inn == "1111111111" and document.buyer.kpp is None


async def test_printed_granted_discount_is_not_added_again(invoice_root, invoice_client):
    """Явная пометка PDF подтверждает учтённую скидку без подгонки суммы строк/итогов."""
    assert included_discounts("Вам предоставлена скидка 10% на 3.00 руб.") == ["3.00"]
    assert included_discounts("Будет предоставлена скидка 10% на 3.00 руб.") == []
    assert included_discounts("Вам предоставлена скидка 10% на " + "9" * 40 + " руб.") == []
    with pymupdf.open() as document:
        document.new_page().insert_htmlbox(
            pymupdf.Rect(30, 30, 550, 200), "Вам предоставлена скидка 10% на 3.00 руб."
        )
        data = document.tobytes()
    page = synthetic_page()
    page.documents[0].totals.adjustments = [
        Adjustment(kind="discount", without_vat="3.00", included_in_line_totals=False)
    ]
    invoice_root.invoice_processor.model.responses[(0, 1)] = page
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("synthetic.pdf", data, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    result = job.result.files[0].documents[0]
    assert result.totals.adjustments[0].without_vat == "3.00"
    assert result.totals.adjustments[0].included_in_line_totals
    assert result.items[0].line_total_without_vat == result.totals.without_vat == "20.00"
    assert result.validation.status == "passed"


def test_source_manufacturer_is_verified_in_physical_cell(tmp_path):
    """Порядок text layer может разрывать слово соседним числом; ячейка доказывает исходное значение."""
    path = tmp_path / "synthetic.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        for left in (30, 130, 230):
            for top, bottom in ((30, 70), (70, 150)):
                page.draw_rect(pymupdf.Rect(left, top, left + 100, bottom))
        for x, label in ((40, "Code"), (140, "Maker"), (240, "Qty")):
            page.insert_text((x, 55), label)
        page.insert_text((40, 90), "001")
        page.insert_text((140, 90), "Synth")
        page.insert_text((240, 90), "7")
        page.insert_text((140, 115), "Manufacturer")
        assert "SynthManufacturer" not in "".join(page.get_text().split())
        document.save(path)
    assert verify_enrichments(path, [{"actual": "SynthManufacturer"}]) == []
    assert verify_enrichments(path, [{"actual": "InventedMaker"}]) == [{"actual": "InventedMaker"}]


async def test_invalid_native_decimal_cannot_escape_output_schema(
    invoice_root, invoice_client, caplog
):
    """После изменений по PDF повторная DTO-валидация блокирует выход числа вне контракта."""
    value = "9" * 30
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files={"files": ("synthetic.pdf", financial_pdf(quantity=value), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.error_code == "invoice_invalid_result" and job.result is None
    assert job.diagnostics and job.diagnostics[0]["file_index"] is None
    assert value not in caplog.text
