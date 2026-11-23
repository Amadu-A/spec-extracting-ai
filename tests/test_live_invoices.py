# tests/test_live_invoices.py
"""Настоящий shared-vlm и реальные локальные PDF; AMQP этим прогоном не проверяется."""

import asyncio
import json
import re
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from time import perf_counter

import pymupdf
import pytest

from app.infrastructure.invoices.vlm import InvoiceVlmClient
from tests.conftest import KEY_A

CORPUS = Path("examples/invoices")
CASES = [p.name for p in sorted(CORPUS.glob("*.pdf"))] or ["corpus-unavailable"]
FIELDS = (
    "caption",
    "article",
    "article_candidate",
    "supplier_code",
    "product_reference_raw",
    "units",
    "quantity",
    "unit_price_without_vat",
    "unit_price_with_vat",
    "line_total_without_vat",
    "vat_rate",
    "vat_amount",
    "line_total_with_vat",
    "row_kind",
    "source_pages",
)


def caption_key(value: str) -> str:
    """Игнорирует физические переносы слов и типографику года/конца предложения, не меняя SKU-символы."""
    value = re.sub(r"\s", "", value).replace("ё", "е")
    value = re.sub(r"(\d{4})г\.", r"\1г", value)
    value = value.replace(",согласно", "согласно")
    return value.rstrip(".")


def verify_enrichments(path: Path, values: list[dict]) -> list[dict]:
    """Дополнительное непустое поле допускается только при подтверждении текстом исходного PDF."""
    with pymupdf.open(path) as document:
        text = "".join(page.get_text() for page in document)
        # В линейном text layer перенос производителя может перемежаться соседними
        # графами. Проверяем также независимые дословные ячейки по геометрии PDF.
        with redirect_stdout(StringIO()):
            cells = [
                cell or ""
                for page in document
                for table in page.find_tables().tables
                for row in table.extract()
                for cell in row
            ]
        text += "\n" + "\n".join(cells)
    compact = re.sub(r"\s", "", text).casefold().replace("ё", "е")
    return [
        value
        for value in values
        if re.sub(r"\s", "", str(value["actual"])).casefold().replace("ё", "е") not in compact
    ]


def compare_files(expected, actual, formatting=None, enrichments=None):
    """Сравнивает физический порядок и значения; расхождения не исправляют эталон/ответ."""
    differences = []
    expected_docs, actual_docs = expected["documents"], actual["documents"]
    if len(expected_docs) != len(actual_docs):
        differences.append(
            dict(field="document_count", expected=len(expected_docs), actual=len(actual_docs))
        )
    for index, (left, right) in enumerate(zip(expected_docs, actual_docs, strict=False)):
        for field in ("document_type", "number", "date", "currency"):
            if left[field] != right[field]:
                differences.append(
                    dict(
                        document_index=index, field=field, expected=left[field], actual=right[field]
                    )
                )
        if len(left["items"]) != len(right["items"]):
            differences.append(
                dict(
                    document_index=index,
                    field="item_count",
                    expected=len(left["items"]),
                    actual=len(right["items"]),
                )
            )
        for row, (a, b) in enumerate(zip(left["items"], right["items"], strict=False), 1):
            if (
                a.get("vat_rate_source") == "document_total"
                and b.get("vat_rate_source") != "document_total"
            ):
                differences.append(
                    dict(
                        document_index=index,
                        row_number=row,
                        field="vat_rate_source",
                        expected="document_total",
                        actual=b.get("vat_rate_source"),
                    )
                )
            for field in FIELDS:
                av, bv = a.get(field), b.get(field)
                if field == "caption":
                    if av != bv and caption_key(av) == caption_key(bv) and formatting is not None:
                        formatting.append(
                            dict(
                                document_index=index,
                                row_number=row,
                                source_pages=b.get("source_pages"),
                                field=field,
                                expected=av,
                                actual=bv,
                            )
                        )
                    av, bv = caption_key(av), caption_key(bv)
                if av != bv:
                    differences.append(
                        dict(
                            document_index=index,
                            row_number=row,
                            source_pages=b.get("source_pages"),
                            field=field,
                            expected=av,
                            actual=bv,
                        )
                    )
            for field in ("brand", "manufacturer"):
                av, bv = a.get(field), b.get(field)
                if av is not None and av != bv:
                    differences.append(
                        dict(
                            document_index=index,
                            row_number=row,
                            field=field,
                            expected=av,
                            actual=bv,
                        )
                    )
                elif av is None and bv is not None and enrichments is not None:
                    enrichments.append(
                        dict(
                            document_index=index,
                            row_number=row,
                            field=field,
                            expected=None,
                            actual=bv,
                        )
                    )
        if left["validation"]["status"] != right["validation"]["status"]:
            differences.append(
                dict(
                    document_index=index,
                    field="validation.status",
                    expected=left["validation"]["status"],
                    actual=right["validation"]["status"],
                )
            )
        for party in ("supplier", "buyer"):
            for field in ("inn", "kpp"):
                av, bv = left[party].get(field), right[party].get(field)
                if av is not None and av != bv:
                    differences.append(
                        dict(
                            document_index=index, field=party + "." + field, expected=av, actual=bv
                        )
                    )
                elif av is None and bv is not None and enrichments is not None:
                    enrichments.append(
                        dict(
                            document_index=index,
                            field=party + "." + field,
                            expected=None,
                            actual=bv,
                        )
                    )
            if enrichments is not None and left[party].get("name") != right[party].get("name"):
                enrichments.append(
                    dict(
                        document_index=index,
                        field=party + ".name",
                        expected=left[party].get("name"),
                        actual=right[party].get("name"),
                    )
                )
        for field in ("without_vat", "vat", "with_vat"):
            if left["totals"][field] != right["totals"][field]:
                differences.append(
                    dict(
                        document_index=index,
                        field="totals." + field,
                        expected=left["totals"][field],
                        actual=right["totals"][field],
                    )
                )
    return differences


@pytest.mark.live
@pytest.mark.corpus
@pytest.mark.parametrize("filename", CASES)
async def test_real_invoice_pdf(invoice_root, invoice_client, request, filename):
    """Загружает реальный файл через REST, выполняет production pipeline и сравнивает с эталоном."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Реальный invoice-прогон требует --vlm")
    paths = await asyncio.to_thread(lambda: sorted(CORPUS.glob("*.pdf")))
    if not paths and not request.config.getoption("--require-invoice-corpus"):
        pytest.skip("Локальный invoice-корпус не установлен")
    assert len(paths) == 7, "Нужны все 7 PDF/JSON в examples/invoices/"
    path = CORPUS / filename
    baseline = json.loads(path.with_suffix(".json").read_text(encoding="utf8"))
    model = InvoiceVlmClient(invoice_root.settings, None)
    invoice_root.invoice_processor.model = model
    invoice_root.settings.vlm_attempts = 3
    output = Path("reports/live/invoices/final")
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    started = perf_counter()
    headers = {"X-API-Key": KEY_A}
    try:
        assert await model.ping(), "Shared VLM недоступна"
        response = await invoice_client.post(
            "/api/v1/invoice-extractions",
            headers=headers,
            files={"files": (path.name, path.read_bytes(), "application/pdf")},
        )
        assert response.status_code == 202, response.text
        identifier = response.json()["job_id"]
        await invoice_root.jobs.execute(identifier)
        job = await invoice_root.repository.get(identifier)
        (output / (path.stem + ".snapshot.json")).write_text(
            job.model_dump_json(indent=2), encoding="utf8"
        )
        status = invoice_root.invoices.status(job)
        result = await invoice_client.get(
            f"/api/v1/invoice-extractions/{identifier}/result.json", headers=headers
        )
        report = dict(
            source=path.name,
            status=status,
            seconds=round(perf_counter() - started, 2),
            live_vlm=True,
            live_amqp=False,
        )
        if result.status_code == 200:
            actual = result.json()
            (output / (path.stem + ".json")).write_text(
                json.dumps(actual, ensure_ascii=False, indent=2), encoding="utf8"
            )
            report["formatting_differences"], report["source_enrichments"] = [], []
            report["differences"] = compare_files(
                baseline["files"][0],
                actual["files"][0],
                report["formatting_differences"],
                report["source_enrichments"],
            )
            report["unverified_enrichments"] = await asyncio.to_thread(
                verify_enrichments, path, report["source_enrichments"]
            )
            report["expected_items"] = baseline["summary"]["item_count"]
            report["actual_items"] = actual["summary"]["item_count"]
        else:
            report["result_error"] = result.json()
        (output / (path.stem + ".comparison.json")).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
        )
        assert result.status_code == 200, (
            f"Ошибка {job.error_code}; диагностика reports/live/invoices"
        )
        assert not report["differences"], (
            f"{len(report['differences'])} различий; reports/live/invoices/{path.stem}.comparison.json"
        )
        assert not report["unverified_enrichments"], (
            "Неподтверждённые дополнительные поля; см. comparison.json"
        )
    finally:
        await model.close()


@pytest.mark.live
@pytest.mark.corpus
async def test_real_three_file_batch_idempotency(invoice_root, invoice_client, request):
    """HTTP-клиент интеграции повторяет тот же POST и получает один реальный пакет из трёх файлов."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Нужен --vlm")
    paths = await asyncio.to_thread(lambda: sorted(CORPUS.glob("*.pdf")))
    if not paths and not request.config.getoption("--require-invoice-corpus"):
        pytest.skip("Локальный invoice-корпус отсутствует")
    assert len(paths) == 7
    records = [
        (
            path,
            json.loads(
                await asyncio.to_thread(path.with_suffix(".json").read_text, encoding="utf8")
            ),
        )
        for path in paths
    ]
    chosen = [
        next((p, b) for p, b in records if b["summary"]["item_count"] == 6),
        next(
            (p, b)
            for p, b in records
            if b["files"][0]["documents"][0]["items"][0]["row_kind"] == "final_payment"
        ),
        next(
            (p, b)
            for p, b in records
            if b["files"][0]["documents"][0]["document_type"] == "specification"
        ),
    ]
    files = [
        ("files", (p.name, await asyncio.to_thread(p.read_bytes), "application/pdf"))
        for p, _ in chosen
    ]
    headers = {"X-API-Key": KEY_A, "Idempotency-Key": "real-three-file-batch"}
    model = InvoiceVlmClient(invoice_root.settings, None)
    invoice_root.invoice_processor.model = model
    invoice_root.settings.vlm_attempts = 3
    try:
        response = await invoice_client.post(
            "/api/v1/invoice-extractions", headers=headers, files=files
        )
        assert response.status_code == 202
        identifier = response.json()["job_id"]
        repeated = await invoice_client.post(
            "/api/v1/invoice-extractions", headers=headers, files=files
        )
        assert repeated.json()["job_id"] == identifier
        await invoice_root.jobs.execute(identifier)
        job = await invoice_root.repository.get(identifier)
        output = Path("reports/live/invoices/api-live-batch")
        await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            (output / "snapshot.json").write_text, job.model_dump_json(indent=2), encoding="utf8"
        )
        assert job.result is not None, job.error_code
        reports = []
        for (p, b), file in zip(chosen, job.result.model_dump()["files"], strict=True):
            formatting, enrichments = [], []
            reports.append(
                dict(
                    file=p.name,
                    differences=compare_files(b["files"][0], file, formatting, enrichments),
                    formatting_differences=formatting,
                    source_enrichments=enrichments,
                )
            )
        await asyncio.to_thread(
            (output / "comparison.json").write_text,
            json.dumps(reports, ensure_ascii=False, indent=2),
            encoding="utf8",
        )
        assert job.result.summary.item_count == 12 and job.result.summary.file_count == 3
        assert not any(report["differences"] for report in reports), (
            "См. reports/live/invoices/api-live-batch"
        )
        downloaded = await invoice_client.get(
            f"/api/v1/invoice-extractions/{identifier}/result.json", headers=headers
        )
        assert downloaded.status_code == 200
    finally:
        await model.close()


@pytest.mark.live
@pytest.mark.corpus
async def test_real_rotated_payment_image(invoice_root, invoice_client, request):
    """Поворот скана и формат PNG сохраняют те же реальные поля, пустые quantity/UOM и одну страницу."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Нужен --vlm")
    paths = await asyncio.to_thread(lambda: sorted(CORPUS.glob("*.json")))
    if not paths and not request.config.getoption("--require-invoice-corpus"):
        pytest.skip("Локальный invoice-корпус отсутствует")
    records = [
        (p, json.loads(await asyncio.to_thread(p.read_text, encoding="utf8"))) for p in paths
    ]
    path, baseline = next(
        (p, b)
        for p, b in records
        if b["files"][0]["documents"][0]["items"][0]["row_kind"] == "final_payment"
    )

    def rotate():
        """Меняет только временную raster-копию оригинала, не переписывая PDF/эталон."""
        with pymupdf.open(path.with_suffix(".pdf")) as document:
            document[0].set_rotation(90)
            scale = invoice_root.settings.render_dpi / 72
            return document[0].get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")

    data = await asyncio.to_thread(rotate)
    model = InvoiceVlmClient(invoice_root.settings, None)
    invoice_root.invoice_processor.model = model
    invoice_root.settings.vlm_attempts = 3
    try:
        response = await invoice_client.post(
            "/api/v1/invoice-extractions",
            headers={"X-API-Key": KEY_A},
            files={"files": ("rotated-payment.png", data, "image/png")},
        )
        assert response.status_code == 202
        identifier = response.json()["job_id"]
        await invoice_root.jobs.execute(identifier)
        job = await invoice_root.repository.get(identifier)
        output = Path("reports/live/invoices/rotated-image")
        await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            (output / "snapshot.json").write_text, job.model_dump_json(indent=2), encoding="utf8"
        )
        assert job.result is not None, job.error_code
        actual = job.result.model_dump()["files"][0]
        differences = compare_files(baseline["files"][0], actual)
        await asyncio.to_thread(
            (output / "comparison.json").write_text,
            json.dumps(differences, ensure_ascii=False, indent=2),
            encoding="utf8",
        )
        assert actual["source_file"] == "rotated-payment.png" and actual["page_count"] == 1
        assert not differences, "См. reports/live/invoices/rotated-image"
    finally:
        await model.close()
