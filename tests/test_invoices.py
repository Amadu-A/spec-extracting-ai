# tests/test_invoices.py
"""Unit/functional/security/regression счетов с реальными PDF/storage/SQL и model double."""

import hashlib
from datetime import timedelta
from io import BytesIO

import pymupdf
import pytest
from openpyxl import load_workbook
from pydantic import ValidationError

from app.domain.errors import ServiceError
from app.domain.invoices.models import InvoiceExport, InvoiceItem, Totals
from app.domain.invoices.validation import validate_document
from app.domain.models import JobState, utcnow
from tests.conftest import KEY_A, KEY_B
from tests.invoice_helpers import synthetic_document

HEADERS = {"X-API-Key": KEY_A}
BASE = "/api/v1/invoice-extractions"


def test_decimal_validation_missing_rounding_and_actual_discrepancy():
    """Большие quantity не превращают допустимое округление цены в ошибку суммы."""
    document = synthetic_document()
    assert validate_document(document).status == "passed"
    document.items[0].quantity, document.items[0].unit_price_without_vat = "610", "1.23"
    document.items[0].line_total_without_vat = "752.00"
    document.totals = Totals(without_vat="752.00", vat="4.00", with_vat="756.00")
    document.items[0].line_total_with_vat = "756.00"
    assert validate_document(document).status == "passed"
    document.items[0].line_total_without_vat = None
    checks = validate_document(document)
    assert checks.status == "partially_checked"
    assert checks.checks[0].calculated is None
    document.totals.with_vat = "999.00"
    assert validate_document(document).status == "failed"
    document.items = [InvoiceItem(row_number=1, caption="Payment", row_kind="final_payment")]
    document.totals = Totals()
    assert validate_document(document).status == "not_checkable"


def test_financial_schema_preserves_nulls_codes_and_rejects_float():
    """Нули и символы кода сохраняются, локализованная сумма нормализуется без float."""
    item = InvoiceItem(
        row_number=1,
        caption="Payment",
        article="00-a/01",
        quantity=None,
        line_total_with_vat="1 234,50",
    )
    assert item.article == "00-a/01" and item.quantity is None and item.units is None
    assert item.line_total_with_vat == "1234.50"
    empty = InvoiceItem(row_number=1, caption="Payment", quantity="", units=" ", brand="")
    assert empty.quantity is None and empty.units is None and empty.brand is None
    for value in (1.25, float("inf"), "NaN", "1e999", True):
        with pytest.raises(ValidationError):
            InvoiceItem(row_number=1, caption="bad", quantity=value)


async def test_batch_api_result_excel_provenance_owner_and_idempotency(
    invoice_root, invoice_client, pdf_bytes
):
    """Проверяет пакет через API, запрет partial export, replay, Excel и обе формы API-key."""
    files = [
        ("files", ("first.pdf", pdf_bytes, "application/pdf")),
        ("files", ("second.pdf", pdf_bytes, "application/pdf")),
    ]
    response = await invoice_client.post(
        BASE, headers={**HEADERS, "Idempotency-Key": "batch-1"}, files=files
    )
    assert response.status_code == 202, response.text
    identifier = response.json()["job_id"]
    assert (
        await invoice_client.get(f"{BASE}/{identifier}/result.json", headers=HEADERS)
    ).status_code == 409
    assert (
        await invoice_client.get(f"{BASE}/{identifier}", headers={"X-API-Key": KEY_B})
    ).status_code == 404
    assert (
        await invoice_client.get(f"/api/v1/extractions/{identifier}", headers=HEADERS)
    ).status_code == 404
    repeated = await invoice_client.post(
        BASE, headers={**HEADERS, "Idempotency-Key": "batch-1"}, files=files
    )
    assert repeated.json()["job_id"] == identifier
    conflict = await invoice_client.post(
        BASE,
        headers={**HEADERS, "Idempotency-Key": "batch-1"},
        files=files,
        data={"page_selection": '{"0":"1"}'},
    )
    assert conflict.status_code == 409
    await invoice_root.jobs.execute(identifier)
    status = await invoice_client.get(f"{BASE}/{identifier}", headers=HEADERS)
    assert status.json()["status"] == "completed", status.text
    assert status.json()["processed_pages"] == 6
    result = InvoiceExport.model_validate(
        (
            await invoice_client.get(
                f"{BASE}/{identifier}/result.json", headers={"Authorization": "Bearer " + KEY_A}
            )
        ).json()
    )
    assert result.summary.item_count == 2 and result.summary.file_count == 2
    assert [f.source_file for f in result.files] == ["first.pdf", "second.pdf"]
    assert result.files[0].source_sha256 == hashlib.sha256(pdf_bytes).hexdigest()
    assert result.files[0].documents[0].attached_pages == [2, 3]
    excel = await invoice_client.get(f"{BASE}/{identifier}/result.xlsx", headers=HEADERS)
    book = load_workbook(BytesIO(excel.content))
    assert book.sheetnames == ["Документы", "Позиции", "Предупреждения"]
    assert book["Позиции"].max_row == 3
    assert book["Позиции"]["G2"].value == "001-A"
    events = await invoice_client.get(f"{BASE}/{identifier}/events", headers=HEADERS)
    assert events.status_code == 200 and '"file_index": 1' in events.text
    assert '"revision": 0' in events.text and "event: job_completed" in events.text
    validated = [
        event.data
        for event in await invoice_root.repository.events(identifier, 0, 10000)
        if event.type == "document_validated"
    ]
    assert [(value["file_index"], value["pdf_page"]) for value in validated] == [(0, 1), (1, 1)]
    preview = await invoice_client.get(
        f"{BASE}/{identifier}/files/1/pages/3/preview", headers=HEADERS
    )
    assert preview.content.startswith(b"\x89PNG")
    assert (
        await invoice_client.get(f"{BASE}/{identifier}/files/8/pages/1/preview", headers=HEADERS)
    ).status_code == 404


async def test_failed_file_blocks_entire_batch_retry_is_scoped(
    invoice_root, invoice_client, pdf_bytes
):
    """Ошибка последнего файла не публикует успешную частичную пачку; retry не дублирует строки."""
    model = invoice_root.invoice_processor.model
    model.responses[(1, 2)] = ServiceError("vlm_unavailable", "Модель недоступна", 503)
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
    assert (await invoice_client.get(f"{BASE}/{identifier}", headers=HEADERS)).json()[
        "status"
    ] == "failed"
    assert (
        await invoice_client.get(f"{BASE}/{identifier}/result.json", headers=HEADERS)
    ).status_code == 409
    calls = len(model.calls)
    model.responses.clear()
    retry = await invoice_client.post(f"{BASE}/{identifier}/files/1/pages/2/retry", headers=HEADERS)
    assert retry.status_code == 202
    await invoice_root.jobs.execute(identifier)
    assert {c["context"]["file_index"] for c in model.calls[calls:]} == {1}
    result = (await invoice_client.get(f"{BASE}/{identifier}/result.json", headers=HEADERS)).json()
    assert result["summary"]["item_count"] == 2
    await invoice_root.jobs.execute(identifier)
    assert len(model.calls) == calls + 3


async def test_session_prompt_csrf_and_profiles_are_isolated(
    invoice_root, invoice_client, pdf_bytes
):
    """Новый prompt не меняет legacy; снимок running job неизменен после PUT/restore."""
    session = (await invoice_client.get("/api/v1/session")).json()
    assert (
        await invoice_client.put(BASE + "/prompt", json={"prompt": "custom"})
    ).status_code == 403
    headers = {"X-CSRF-Token": session["csrf"]}
    legacy = (await invoice_client.get("/api/v1/session/prompt")).json()
    changed = await invoice_client.put(
        BASE + "/prompt", json={"prompt": "custom invoice"}, headers=headers
    )
    assert changed.status_code == 200
    response = await invoice_client.post(
        BASE, headers=headers, files={"files": ("a.pdf", pdf_bytes, "application/pdf")}
    )
    job = await invoice_root.repository.get(response.json()["job_id"])
    assert job.prompt == "custom invoice"
    assert job.prompt_version == changed.json()["version"]
    assert (await invoice_client.get("/api/v1/session/prompt")).json() == legacy
    await invoice_client.post(BASE + "/prompt/restore", headers=headers)
    assert (await invoice_root.repository.get(job.id)).prompt == "custom invoice"
    assert (await invoice_client.get(BASE + "/prompt", headers=HEADERS)).status_code == 403


async def test_images_real_normalization_and_corrupt_upload(
    invoice_root, invoice_client, pdf_bytes
):
    """Настоящий JPEG/PNG проходит общий renderer; MIME spoof и повреждённые файлы отклоняются."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as pdf:
        pix = pdf[0].get_pixmap(matrix=pymupdf.Matrix(0.5, 0.5))
        images = [
            ("page.png", pix.tobytes("png"), "image/png"),
            ("page.jpg", pix.tobytes("jpeg"), "image/jpeg"),
        ]
    response = await invoice_client.post(
        BASE, headers=HEADERS, files=[("files", image) for image in images]
    )
    assert response.status_code == 202, response.text
    job = await invoice_root.repository.get(response.json()["job_id"])
    assert all(s.page_count == 1 for s in job.sources)
    await invoice_root.jobs.execute(job.id)
    assert (await invoice_root.repository.get(job.id)).state == JobState.COMPLETED
    for name, data, mime in [
        ("fake.png", pdf_bytes, "image/png"),
        ("bad.pdf", b"%PDF-broken", "application/pdf"),
        ("bad.png", b"\x89PNG\r\n\x1a\nbroken", "image/png"),
    ]:
        assert (
            await invoice_client.post(BASE, headers=HEADERS, files={"files": (name, data, mime)})
        ).status_code in (415, 422)


async def test_batch_ttl_protects_all_originals_and_terminal_cleanup(
    invoice_root, invoice_client, pdf_bytes
):
    """Один живой batch защищает каждый оригинал; expiry даёт 410 до очистки и удаляет файлы."""
    response = await invoice_client.post(
        BASE,
        headers=HEADERS,
        files=[
            ("files", ("a.pdf", pdf_bytes, "application/pdf")),
            ("files", ("b.pdf", pdf_bytes, "application/pdf")),
        ],
    )
    identifier = response.json()["job_id"]
    job = await invoice_root.repository.get(identifier)
    for source in job.sources:
        document = await invoice_root.repository.document(source.document_id)
        assert document.expires_at == job.expires_at

    def expire(current):
        """Имитирует истёкшее terminal задание без ожидания реального часа."""
        current.state = JobState.CANCELLED
        current.expires_at = utcnow() - timedelta(seconds=1)

    await invoice_root.repository.edit(identifier, expire)
    assert (await invoice_client.get(f"{BASE}/{identifier}", headers=HEADERS)).status_code == 410
    await invoice_root.extractions.purge_expired()
    assert all(not invoice_root.storage._directory(s.document_id).exists() for s in job.sources)
