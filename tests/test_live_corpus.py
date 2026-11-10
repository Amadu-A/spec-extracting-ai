# tests/test_live_corpus.py
"""Полные четыре PDF через настоящий shared-vlm и REST/use-case с отдельной SQLite.

Запускается явно с --vlm --require-corpus. RabbitMQ здесь заменён прямым вызовом
штатного processor: этот прогон не доказывает работоспособность реального AMQP.
Содержимое документов и результаты сохраняются только в ignored reports/live.
"""

import asyncio
import json
from io import BytesIO
from pathlib import Path
from time import perf_counter

import pymupdf
import pytest
from openpyxl import load_workbook

from app.domain.models import Export
from app.domain.quality import compare_exports, compare_reviewed_fields
from app.infrastructure.vlm import SharedVlmClient
from scripts.evaluate import find_pairs
from tests.conftest import KEY_A


@pytest.mark.live
@pytest.mark.corpus
async def test_all_documents_with_real_vlm(root, client, request):
    """Проверяет полный corpus и реальные JSON/XLSX без подмены ответа модели."""
    if not (request.config.getoption("--vlm") and request.config.getoption("--require-corpus")):
        pytest.skip("Полный прогон требует --vlm --require-corpus")
    manifest = json.loads(
        await asyncio.to_thread(
            Path("tests/fixtures/corpus-manifest.json").read_text, encoding="utf8"
        )
    )
    samples = json.loads(
        await asyncio.to_thread(
            Path("tests/fixtures/additional-fields.json").read_text, encoding="utf8"
        )
    )
    output = Path("reports/live/extended-fields")
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=True)
    model = SharedVlmClient(root.settings)
    root.processor.model = model
    root.settings.vlm_attempts = 3
    headers = {"X-API-Key": KEY_A}
    reports, failures = [], []
    try:
        assert await model.ping()
        for pdf, baseline in find_pairs(Path("examples")):
            name = pdf.stem.removeprefix("spec_")
            started = perf_counter()
            with pdf.open("rb") as source:
                response = await client.post(
                    "/api/v1/extractions",
                    headers=headers,
                    files={"file": (pdf.name, source, "application/pdf")},
                    data={
                        "pages": f"1-{manifest[name]['pdf_pages']}",
                        "project_code": manifest[name]["project_code"],
                    },
                )
            assert response.status_code == 202
            identifier = response.json()["job_id"]
            await root.processor.execute(identifier)
            job = await root.repository.get(identifier)
            actual = job.export()
            (output / f"{name}.json").write_text(actual.model_dump_json(indent=2), encoding="utf8")
            (output / f"{name}.raw.json").write_text(
                json.dumps(
                    {
                        "prompt_version": job.prompt_version,
                        "pages": [
                            {"pdf_page": page.pdf_page, "raw_text": page.raw_text}
                            for page in job.pages
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf8",
            )
            (output / f"{name}.diagnostics.json").write_text(
                json.dumps(
                    {
                        "status": root.extractions.status(job),
                        "warnings": [
                            warning.model_dump(mode="json")
                            for page in job.pages
                            for warning in page.warnings
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf8",
            )
            expected = Export.model_validate_json(baseline.read_text(encoding="utf-8-sig"))
            reviewed = compare_reviewed_fields(samples[name], actual)
            reports.append(
                {
                    "name": name,
                    "status": job.state,
                    "duration_seconds": round(perf_counter() - started, 2),
                    "prompt_version": job.prompt_version,
                    "reviewed_additional_fields": reviewed,
                    **compare_exports(expected, actual),
                }
            )
            (output / "quality.json").write_text(
                json.dumps(
                    {
                        "mode": "реальный shared-vlm → REST/use-case → SQLite; без AMQP",
                        "datasets": reports,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf8",
            )
            if job.state not in ("completed", "completed_with_warnings"):
                failures.append(f"{name}: {job.state}, {job.error_code}")
                continue
            json_response = await client.get(
                f"/api/v1/extractions/{identifier}/result.json", headers=headers
            )
            excel_response = await client.get(
                f"/api/v1/extractions/{identifier}/result.xlsx", headers=headers
            )
            assert json_response.status_code == excel_response.status_code == 200
            assert json_response.json() == actual.model_dump()
            (output / f"{name}.xlsx").write_bytes(excel_response.content)
            book = load_workbook(BytesIO(excel_response.content))
            sheet = book.active
            assert book.sheetnames == ["Лист_1"] and sheet.max_column == 18
            assert sheet.max_row == len(actual.items) + 1
            for index, item in enumerate(actual.items, start=2):
                for field, column in {
                    "Caption": "C",
                    "Units": "D",
                    "Quantity": "E",
                    "Brand": "M",
                    "Article": "N",
                    "UserComment1": "P",
                    "Pages": "Q",
                    "Note": "R",
                }.items():
                    assert (sheet[f"{column}{index}"].value or "") == getattr(item, field)
            if reviewed["located_rows"] != reviewed["reviewed_rows"] or any(
                count != reviewed["reviewed_rows"] for count in reviewed["exact_fields"].values()
            ):
                failures.append(f"{name}: расхождения ручной выборки, см. ignored quality.json")
    finally:
        await model.close()
    assert len(reports) == 4
    assert not failures, "; ".join(failures)


@pytest.mark.live
async def test_real_vlm_article_column_and_leading_zeros(root, client, request):
    """Настоящая модель читает явно названную графу Артикул в синтетической PDF-таблице."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Требуется --vlm")

    def source_pdf() -> bytes:
        """Создаёт независимый тестовый документ с кириллицей через встроенный HTML renderer."""
        with pymupdf.open() as document:
            page = document.new_page(width=1200, height=450)
            page.insert_htmlbox(
                pymupdf.Rect(30, 30, 1170, 400),
                """
                <h2>Спецификация оборудования</h2>
                <table border="1" cellpadding="8" style="font-size:16px">
                <tr><th>Наименование</th><th>Тип, марка</th><th>Завод-изготовитель</th>
                <th>Артикул</th><th>Единица измерения</th><th>Количество</th><th>Примечание</th></tr>
                <tr><td>Клапан</td><td>DN100</td><td>Ридан</td><td>00042/AB-1</td>
                <td>шт</td><td>2,50</td><td>Для ИТП</td></tr></table>
                """,
            )
            return document.tobytes()

    model = SharedVlmClient(root.settings)
    root.processor.model = model
    try:
        response = await client.post(
            "/api/v1/extractions",
            headers={"X-API-Key": KEY_A},
            files={
                "file": (
                    "synthetic-article.pdf",
                    await asyncio.to_thread(source_pdf),
                    "application/pdf",
                )
            },
            data={"pages": "1", "project_code": "SYNTHETIC"},
        )
        assert response.status_code == 202
        identifier = response.json()["job_id"]
        await root.processor.execute(identifier)
        job = await root.repository.get(identifier)
        output = Path("reports/live/extended-fields/synthetic-article.json")
        await asyncio.to_thread(output.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            output.write_text, job.export().model_dump_json(indent=2), encoding="utf8"
        )
        assert job.state in ("completed", "completed_with_warnings")
        assert job.export().model_dump() == {
            "items": [
                {
                    "Caption": "Клапан DN100 Ридан",
                    "Units": "шт",
                    "Quantity": "2,50",
                    "UserComment1": "SYNTHETIC",
                    "Pages": "1",
                    "Brand": "Ридан",
                    "Article": "00042/AB-1",
                    "Note": "Для ИТП",
                }
            ]
        }
    finally:
        await model.close()


@pytest.mark.live
async def test_real_tm_page_nine_bounded_warnings(root, client, request):
    """Реальный лист ТМ-9 не расходует ответ на зацикленное предупреждение об OCR."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Требуется --vlm")
    pairs = await asyncio.to_thread(find_pairs, Path("examples"))
    source = next((pdf for pdf, _ in pairs if pdf.stem.endswith(".ТМ")), None)
    if source is None:
        pytest.skip("Нужен локальный ТМ в examples/")
    model = SharedVlmClient(root.settings)
    root.processor.model = model
    try:
        with source.open("rb") as stream:
            response = await client.post(
                "/api/v1/extractions",
                headers={"X-API-Key": KEY_A},
                files={"file": (source.name, stream, "application/pdf")},
                data={"pages": "9", "project_code": "267-РД-ИТП.ТМ"},
            )
        assert response.status_code == 202
        identifier = response.json()["job_id"]
        await root.processor.execute(identifier)
        job = await root.repository.get(identifier)
        assert job.state in ("completed", "completed_with_warnings"), job.error_code
        assert job.export().items and all(item.Pages == "9" for item in job.export().items)
        assert all(
            len(warning.message) <= 600
            for warning in job.pages[0].warnings
            if warning.code == "model_warning"
        )
        assert all(item.Article == "" for item in job.export().items)
    finally:
        await model.close()
