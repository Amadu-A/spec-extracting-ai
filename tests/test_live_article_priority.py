# tests/test_live_article_priority.py
"""Настоящий shared-vlm: два источника Article, приоритет и границы объединённой ячейки."""

import asyncio
from pathlib import Path

import pymupdf
import pytest

from app.infrastructure.vlm import SharedVlmClient
from tests.conftest import KEY_A


def article_pdf(case: str) -> bytes:
    """Создаёт настоящий PDF с разными заголовками и независимыми контрольными значениями."""
    article_column = case in ("both", "both_empty")
    code_column = case != "neither"
    headers = ["Наименование", "Тип, марка, обозначение документа", "Завод-изготовитель"]
    if article_column:
        headers.append("Артикул")
    if code_column:
        headers.append("Код оборудования, изделия, материала")
    headers.extend(["Единица измерения", "Количество", "Примечание"])
    rows = []
    for index, caption in enumerate(
        ("Клапан", "Задвижка", "Фильтр") if case == "merged" else ("Клапан",)
    ):
        cells = [caption, "DN100", "Ридан"]
        if article_column:
            cells.append("00042/Ab-1" if case == "both" else "")
        if code_column:
            if case == "merged":
                cells.append("00042/Ab-1" if index == 0 else "")
            else:
                code = (
                    "CATALOG-999"
                    if article_column
                    else ("" if case == "code_empty" else "00042/Ab-1")
                )
                cells.append(code)
        cells.extend(["шт", "2,50", "Для ИТП"])
        rows.append(cells)
    with pymupdf.open() as document:
        page = document.new_page(width=1400, height=500)
        page.insert_htmlbox(pymupdf.Rect(25, 10, 1375, 55), "<h2>Спецификация оборудования</h2>")
        width, top, height = 1350 / len(headers), 65, 85
        for number, cells in enumerate([headers, *rows]):
            for column, text in enumerate(cells):
                merged = case == "merged" and column == 3
                if merged and number == 2:
                    continue
                cell = pymupdf.Rect(
                    25 + column * width,
                    top + number * height,
                    25 + (column + 1) * width,
                    top + (number + (2 if merged and number == 1 else 1)) * height,
                )
                page.draw_rect(cell, width=1)
                page.insert_htmlbox(
                    cell + (5, 5, -5, -5),
                    f'<span style="font-size:16px">{text}</span>',
                )
        return document.tobytes()


@pytest.mark.live
@pytest.mark.parametrize(
    "case", ["code_only", "both", "both_empty", "code_empty", "neither", "merged"]
)
async def test_real_article_priority_and_merged_cells(root, client, request, case):
    """Проверяет настоящие ответы модели, включая пустой приоритетный Артикул и rowspan."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Требуется --vlm")
    model = SharedVlmClient(root.settings)
    root.processor.model = model
    try:
        response = await client.post(
            "/api/v1/extractions",
            headers={"X-API-Key": KEY_A},
            files={
                "file": (
                    f"{case}.pdf",
                    await asyncio.to_thread(article_pdf, case),
                    "application/pdf",
                )
            },
            data={"pages": "1", "project_code": "SYNTHETIC"},
        )
        assert response.status_code == 202
        identifier = response.json()["job_id"]
        await root.processor.execute(identifier)
        job = await root.repository.get(identifier)
        target = Path(f"reports/live/article-sources/synthetic-{case}.json")
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(
            target.write_text, job.export().model_dump_json(indent=2), encoding="utf8"
        )
        assert job.state in ("completed", "completed_with_warnings"), job.error_code
        captions = ("Клапан", "Задвижка", "Фильтр") if case == "merged" else ("Клапан",)
        expected = []
        for index, caption in enumerate(captions):
            article = "00042/Ab-1" if case in ("code_only", "both", "merged") else ""
            if case == "merged" and index == 2:
                article = ""
            expected.append(
                {
                    "Caption": caption + " DN100 Ридан",
                    "Units": "шт",
                    "Quantity": "2,50",
                    "UserComment1": "SYNTHETIC",
                    "Pages": "1",
                    "Brand": "Ридан",
                    "Article": article,
                    "Note": "Для ИТП",
                }
            )
        assert job.export().model_dump() == {"items": expected}
    finally:
        await model.close()
