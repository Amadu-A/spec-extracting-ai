# tests/test_file_name.py
"""Имя исходного PDF добавляется сервером во все результаты без участия VLM."""

import json
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.domain.models import Export
from tests.conftest import KEY_A, KEY_B
from tests.test_domain import page, row

HEADERS = {"X-API-Key": KEY_A}


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("267-РД-ИТП.АТМД.pdf", "267-РД-ИТП.АТМД.pdf"),
        ("../private/Проект.pdf", "Проект.pdf"),
        (r"C:\private\Проект.pdf", "Проект.pdf"),
        ("=1+1.pdf", "=1+1.pdf"),
        ("Документ «А».PDF", "Документ «А».PDF"),
    ],
)
async def test_source_name_in_queued_completed_snapshot_json_and_excel(
    root, client, pdf_bytes, filename, expected
):
    """Путь не раскрывается; имя из multipart, подмена формы и текст VLM не меняют источник."""
    uploaded = await client.post(
        "/api/v1/documents",
        headers=HEADERS,
        files={"file": (filename, pdf_bytes, "application/pdf")},
    )
    assert uploaded.status_code == 201
    created = await client.post(
        "/api/v1/extractions",
        headers=HEADERS,
        data={
            "document_id": uploaded.json()["id"],
            "pages": "1-2",
            "project_code": "P",
            "file_name": "Поддельный источник.pdf",
        },
    )
    assert created.status_code == 202
    identifier = created.json()["job_id"]
    prefix = f"/api/v1/extractions/{identifier}"
    queued = (await client.get(prefix + "/snapshot", headers=HEADERS)).json()
    assert queued["partial"] == {"file_name": expected, "items": []}
    root.model.responses = [
        page([row(caption="Название из модели.pdf")]).model_dump_json(),
        page([row(caption="Вторая позиция")]).model_dump_json(),
    ]
    before = await root.repository.get(identifier)
    await root.processor.execute(identifier)
    result = (await client.get(prefix + "/result.json", headers=HEADERS)).json()
    assert list(result) == ["file_name", "items"] and result["file_name"] == expected
    assert len(result["items"]) == 2 and all(len(item) == 8 for item in result["items"])
    snapshot = (await client.get(prefix + "/snapshot", headers=HEADERS)).json()
    assert snapshot["partial"] == result
    excel = await client.get(prefix + "/result.xlsx", headers=HEADERS)
    sheet = load_workbook(BytesIO(excel.content)).active
    assert sheet["P1"].value == "Источник/Наименование файла"
    for index, item in enumerate(result["items"], 2):
        assert sheet[f"P{index}"].value == expected and sheet[f"P{index}"].data_type == "s"
        assert sheet[f"Q{index}"].value == item["UserComment1"]
        assert sheet[f"R{index}"].value == item["Pages"]
        assert (sheet[f"S{index}"].value or "") == item["Note"]
    assert all(call["prompt"] == before.prompt for call in root.model.calls)
    assert "file_name" not in json.dumps(root.model.calls)
    denied = await client.get(prefix + "/result.json", headers={"X-API-Key": KEY_B})
    assert denied.status_code == 404 and expected not in denied.text


async def test_same_pdf_with_different_names_keeps_independent_sources(root, client, pdf_bytes):
    """Имя принадлежит заданию; одинаковый PDF в другом задании его не подменяет."""
    for name in ("Первый.pdf", "Второй.pdf"):
        response = await client.post(
            "/api/v1/extractions",
            headers=HEADERS,
            files={"file": (name, pdf_bytes, "application/pdf")},
            data={"pages": "1", "project_code": "P"},
        )
        identifier = response.json()["job_id"]
        await root.processor.execute(identifier)
        loaded = await root.repository.get(identifier)
        assert loaded.export().file_name == name
        result = (
            await client.get(f"/api/v1/extractions/{identifier}/result.json", headers=HEADERS)
        ).json()
        assert result["file_name"] == name


def test_legacy_export_and_untrusted_filename_types():
    """Старые эталоны читаются без file_name; новое поле не принимает null или число."""
    from pydantic import ValidationError

    assert Export.model_validate({"items": []}).model_dump() == {"file_name": "", "items": []}
    assert Export.model_json_schema(mode="serialization")["required"] == ["file_name", "items"]
    for value in (None, 123, ["file.pdf"]):
        with pytest.raises(ValidationError):
            Export.model_validate({"file_name": value, "items": []})
