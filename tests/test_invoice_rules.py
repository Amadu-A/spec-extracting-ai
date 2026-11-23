# tests/test_invoice_rules.py
"""Регрессии финансов, переносов и нескольких документов на синтетических источниках."""

import asyncio
import struct
from datetime import timedelta
from io import BytesIO

from openpyxl import load_workbook

from app.domain.invoices.models import (
    Adjustment,
    Fragment,
    Totals,
)
from app.domain.invoices.validation import validate_document
from app.domain.models import JobState, utcnow
from tests.invoice_helpers import synthetic_document, synthetic_page
from tests.test_invoices import BASE, HEADERS


def test_adjustments_already_in_price_are_not_subtracted_twice():
    """Явная внешняя доставка учитывается один раз, уже учтённая скидка — ни разу повторно."""
    document = synthetic_document()
    document.totals = Totals(
        without_vat="30.00",
        vat="6.00",
        with_vat="36.00",
        adjustments=[
            Adjustment(
                kind="delivery",
                without_vat="10.00",
                vat="2.00",
                with_vat="12.00",
                included_in_line_totals=False,
            ),
            Adjustment(
                kind="discount",
                without_vat="-5.00",
                vat="-1.00",
                with_vat="-6.00",
                included_in_line_totals=True,
            ),
        ],
    )
    assert validate_document(document).status == "passed"
    assert document.items[0].line_total_with_vat == "24.00"


async def test_mixed_document_vat_does_not_replace_row_rates_or_fill_unknown(
    invoice_root, invoice_client, pdf_bytes
):
    """При разных подтверждённых ставках общая ставка не подставляется в пустую строку."""
    first = synthetic_page()
    fragment = first.documents[0]
    fragment.document_vat_rate = "20"
    for index, rate in ((2, "22"), (3, None)):
        row = fragment.rows[0].model_copy(deep=True)
        row.row_id = f"table1/row{index}"
        row.item.row_number, row.item.vat_rate = index, rate
        fragment.rows.append(row)
    second = synthetic_page(2)
    second.role = "invoice"
    second.documents = [Fragment(document_vat_rate="22")]
    invoice_root.invoice_processor.model.responses.update({(0, 1): first, (0, 2): second})
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("synthetic.pdf", pdf_bytes, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.result is not None, job.error_code
    items = job.result.files[0].documents[0].items
    assert [item.vat_rate for item in items] == ["20", "22", None]
    assert items[-1].vat_rate_source is None


async def test_cross_page_row_duplicate_product_and_document_boundaries(
    invoice_root, invoice_client, pdf_bytes
):
    """Склеивается только реальный перенос; одинаковый код в разных строках/документах сохраняется."""
    first = synthetic_page()
    first.documents[0].rows[0].continues_next = True
    second = synthetic_page()
    second.documents[0].rows[0].continuation = True
    second.documents[0].rows[0].item.caption = "continued"
    duplicate = second.documents[0].rows[0].model_copy(deep=True)
    duplicate.continuation = False
    duplicate.row_id = "row2"
    duplicate.item.row_number = 2
    second.documents[0].rows.append(duplicate)
    third = synthetic_page()
    third.documents[0].starts_document = True
    third.documents[0].number = "0002/26"
    model = invoice_root.invoice_processor.model
    model.responses = {(0, 1): first, (0, 2): second, (0, 3): third}
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("source.pdf", pdf_bytes, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.result is not None, job.error_code
    documents = job.result.files[0].documents
    assert len(documents) == 2
    assert [len(d.items) for d in documents] == [2, 1]
    assert documents[0].items[0].source_pages == [1, 2]
    assert documents[0].items[0].caption.endswith("continued")
    assert documents[0].items[0].article == documents[0].items[1].article
    assert documents[0].validation.status == "failed"
    assert job.state == JobState.WARNINGS


async def test_partial_selection_is_warned_and_uncertain_page_fails(
    invoice_root, invoice_client, pdf_bytes
):
    """Исключённый лист виден как warning, неполностью прочитанный выбранный лист блокирует экспорт."""
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files={"files": ("a.pdf", pdf_bytes, "application/pdf")},
        data={"page_selection": '{"0":"1"}'},
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    assert job.state == JobState.WARNINGS
    assert any("не все страницы" in w["message"] for w in job.diagnostics)
    page = synthetic_page()
    page.completeness = "uncertain"
    invoice_root.invoice_processor.model.responses[(0, 1)] = page
    await invoice_root.invoices.retry_file(identifier, "api:alpha", 0, 1)
    await invoice_root.jobs.execute(identifier)
    assert (await invoice_root.repository.get(identifier)).state == JobState.FAILED


async def test_cancel_resume_fencing_and_no_duplicate_delivery(
    invoice_root, invoice_client, pdf_bytes
):
    """Общий lease защищает от параллельной доставки; queued cancel не вызывает VLM."""
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("a.pdf", pdf_bytes, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    model = invoice_root.invoice_processor.model
    await asyncio.gather(
        invoice_root.jobs.execute(identifier), invoice_root.jobs.execute(identifier)
    )
    assert len(model.calls) == 3
    job = await invoice_root.repository.get(identifier)
    assert job.result.summary.item_count == 1

    # Эмуляция падения после committed страниц, до общей финализации.
    def interrupted(current):
        """Сохраняет checkpoints и просроченный lease старого worker."""
        current.state, current.result = JobState.RUNNING, None
        current.lease_token = "old"
        current.lease_until = utcnow() - timedelta(seconds=1)
        current.expires_at = utcnow() + timedelta(
            seconds=invoice_root.settings.job_timeout_seconds
            + invoice_root.settings.retention_seconds
        )

    await invoice_root.repository.edit(identifier, interrupted)
    await invoice_root.jobs.execute(identifier)
    assert len(model.calls) == 3
    assert (await invoice_root.repository.get(identifier)).result.summary.item_count == 1
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("b.pdf", pdf_bytes, "application/pdf")}
    )
    cancelled = response.json()["job_id"]
    for _ in range(2):
        assert (await invoice_client.post(f"{BASE}/{cancelled}/cancel", headers=HEADERS)).json()[
            "status"
        ] == "cancelled"
    await invoice_root.jobs.execute(cancelled)
    assert len(model.calls) == 3


async def test_size_page_limits_password_and_pixel_bomb(invoice_root, invoice_client, pdf_bytes):
    """Пределы пакета/страниц, реальный password PDF и IHDR pixel bomb дают явные ошибки."""
    import pymupdf

    invoice_root.settings.invoice_max_files = 1
    files = [("files", ("a.pdf", pdf_bytes, "application/pdf"))] * 2
    assert (await invoice_client.post(BASE, headers=HEADERS, files=files)).status_code == 413
    invoice_root.settings.max_selected_pages = 2
    assert (await invoice_client.post(BASE, headers=HEADERS, files=files[:1])).status_code in (
        413,
        422,
    )
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        encrypted = pdf.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="synthetic-owner",
            user_pw="synthetic-user",
        )
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("locked.pdf", encrypted, "application/pdf")}
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "password_pdf"
    bomb = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 100000, 100000)
        + b"\x08\x02\x00\x00\x00"
    )
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("huge.png", bomb, "image/png")}
    )
    assert response.status_code == 422 and response.json()["error"]["code"] == "image_pixel_limit"


async def test_invoice_excel_text_formula_and_null_cells(invoice_root, invoice_client, pdf_bytes):
    """Новая книга хранит пользовательский текст буквально, пустые деньги не становятся нулём."""
    page = synthetic_page()
    page.documents[0].rows[0].item.caption = '=HYPERLINK("http://example.invalid")'
    page.documents[0].rows[0].item.quantity = None
    invoice_root.invoice_processor.model.responses[(0, 1)] = page
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("=source.pdf", pdf_bytes, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    data = (await invoice_client.get(f"{BASE}/{identifier}/result.xlsx", headers=HEADERS)).content
    sheet = load_workbook(BytesIO(data))["Позиции"]
    assert sheet["A2"].data_type == "s" and sheet["D2"].data_type == "s"
    assert sheet["M2"].value is None


def test_nested_openapi_schema_and_legacy_item_contract(invoice_root):
    """Swagger раскрывает финансовые вложенности и сохраняет прежние восемь item-полей."""
    from app.domain.models import SpecificationItem
    from app.presentation.http.app import create_app

    schema = create_app(invoice_root).openapi()
    assert BASE in schema["paths"]
    models = schema["components"]["schemas"]
    assert {
        "InvoiceExport",
        "InvoiceFile",
        "InvoiceDocument",
        "InvoiceItem",
        "Validation",
        "Check",
    } <= models.keys()
    assert set(SpecificationItem.model_fields) == {
        "Caption",
        "Units",
        "Quantity",
        "UserComment1",
        "Pages",
        "Brand",
        "Article",
        "Note",
    }


async def test_parallel_session_prompt_updates_preserve_both_profiles(invoice_root):
    """Два устаревших чтения сессии не могут стереть prompt другого профиля."""
    _, session = await invoice_root.sessions.create()
    first = await invoice_root.repository.session(session.token_hash)
    second = await invoice_root.repository.session(session.token_hash)
    await asyncio.gather(
        invoice_root.sessions.prompt(first, value="Specification-only"),
        invoice_root.invoices.prompt(second, value="Invoice-only"),
    )
    saved = await invoice_root.repository.session(session.token_hash)
    assert saved.prompt == "Specification-only" and saved.invoice_prompt == "Invoice-only"
    assert saved.expires_at == session.expires_at


async def test_manual_retry_limit_is_independent_of_vlm_attempts(
    invoice_root, invoice_client, pdf_bytes
):
    """Ограничение действует и при отказе до первого model request."""
    invoice_root.settings.invoice_max_page_retries = 1
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("synthetic.pdf", pdf_bytes, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await invoice_root.invoices.cancel(identifier, "api:alpha")
    await invoice_root.invoices.retry_file(identifier, "api:alpha", 0, 1)
    await invoice_root.invoices.cancel(identifier, "api:alpha")
    again = await invoice_client.post(f"{BASE}/{identifier}/files/0/pages/1/retry", headers=HEADERS)
    assert again.status_code == 409 and again.json()["error"]["code"] == "retry_limit"


async def test_snapshot_after_retry_keeps_raw_in_selected_file(
    invoice_root, invoice_client, pdf_bytes
):
    """Retry первого файла не подставляет в viewer старый raw последнего файла пакета."""
    model = invoice_root.invoice_processor.model
    first, second = synthetic_page(3), synthetic_page(3)
    first.warnings, second.warnings = ["FIRST_FILE"], ["SECOND_FILE"]
    model.responses[(0, 3)], model.responses[(1, 3)] = first, second
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files=[
            ("files", ("a.pdf", pdf_bytes, "application/pdf")),
            ("files", ("b.pdf", pdf_bytes, "application/pdf")),
        ],
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    await invoice_root.invoices.retry_file(identifier, "api:alpha", 0, 1)
    snapshot = (await invoice_client.get(f"{BASE}/{identifier}/snapshot", headers=HEADERS)).json()
    assert snapshot["raw_text"] == ""
    await invoice_root.jobs.execute(identifier)
    snapshot = (await invoice_client.get(f"{BASE}/{identifier}/snapshot", headers=HEADERS)).json()
    assert "FIRST_FILE" in snapshot["raw_text"] and "SECOND_FILE" not in snapshot["raw_text"]


async def test_contract_attachment_totals_do_not_change_invoice_finances(
    invoice_root, invoice_client, pdf_bytes
):
    """Суммы соглашения и его корректировки не входят в арифметику счёта."""
    page = synthetic_page(2)
    page.documents = [
        Fragment(
            totals=Totals(
                without_vat="999.00",
                adjustments=[
                    Adjustment(kind="other", without_vat="100.00", included_in_line_totals=False)
                ],
            )
        )
    ]
    invoice_root.invoice_processor.model.responses[(0, 2)] = page
    response = await invoice_client.post(
        BASE, headers=HEADERS, files={"files": ("synthetic.pdf", pdf_bytes, "application/pdf")}
    )
    identifier = response.json()["job_id"]
    await invoice_root.jobs.execute(identifier)
    job = await invoice_root.repository.get(identifier)
    document = job.result.files[0].documents[0]
    assert document.totals.without_vat == "20.00" and document.totals.adjustments == []
    assert document.validation.status == "passed"
