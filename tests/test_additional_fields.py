# tests/test_additional_fields.py
"""Восемь полей, переносы и совместимость durable результатов и событий прежней версии."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.models import Event, Export, Warning
from app.domain.reconcile import parse_model_response, reconcile
from app.infrastructure.database import EventRecord, JobRecord
from tests.test_api import HEADERS, create
from tests.test_domain import page, row

FIELDS = ["Caption", "Units", "Quantity", "UserComment1", "Pages", "Brand", "Article", "Note"]


@pytest.mark.parametrize("extra", [{}, {"brand": "Ридан", "article": "00042/AB-1", "note": "К1"}])
def test_optional_columns_and_unchanged_caption(extra):
    """Отсутствующие графы пусты; изготовитель остаётся и в Caption, и в Brand."""
    response = page([row(caption="Клапан", designation="DN100", manufacturer="Ридан", **extra)])
    response.article_header = "Артикул" if extra else ""
    parsed = parse_model_response(response.model_dump_json())
    items, warnings, _ = reconcile(3, "P", parsed, None)
    item = items[0].item.model_dump()
    assert not warnings and list(item) == FIELDS
    assert item["Caption"] == "Клапан DN100 Ридан"
    for field in ("Brand", "Article", "Note"):
        assert item[field] == extra.get(field.lower(), "")
    assert all(isinstance(value, str) for value in item.values())


@pytest.mark.parametrize("field", ["Brand", "Article", "Note"])
@pytest.mark.parametrize("value", [None, 10, [], {}])
def test_new_fields_reject_non_strings(field, value):
    """Новые значения не допускают неявное преобразование null/чисел/контейнеров."""
    data = {"Caption": "Клапан", "Units": "шт", "Quantity": "2", "UserComment1": "P", "Pages": "1"}
    with pytest.raises(ValidationError):
        Export.model_validate({"items": [{**data, field: value}]})
    with pytest.raises(ValidationError):
        row(**{field.lower(): value})


@pytest.mark.parametrize("origin", ["previous", "next", "both"])
def test_new_values_survive_adjacent_carryover(origin):
    """Перенос сохраняет значения с любой половины строки и физическую страницу начала."""
    extra = {"brand": "Ридан", "article": "00042/AB", "note": "К1"}
    previous = reconcile(
        2,
        "P",
        page(
            [row(caption="Клапан", continues_next=True, **(extra if origin != "next" else {}))],
            article_header="Артикул",
        ),
        None,
    )[2]
    items, warnings, carry = reconcile(
        3,
        "P",
        page([row(caption="DN100", continuation=True, **(extra if origin != "previous" else {}))]),
        previous,
    )
    assert not warnings and not carry.pending
    assert items[0].item.Caption == "Клапан DN100"
    assert items[0].source_pages == [2, 3] and items[0].item.Pages == "2"
    assert {field: getattr(items[0].item, field.title()) for field in extra} == extra


@pytest.mark.parametrize("field", ["brand", "article", "note"])
def test_conflicting_additional_fields_are_diagnostics(field):
    """Несовместимые значения не теряются при произвольном выборе одной половины."""
    previous = reconcile(
        1,
        "P",
        page([row(continues_next=True, **{field: "начало"})], article_header="Артикул"),
        None,
    )[2]
    items, warnings, carry = reconcile(
        2, "P", page([row(continuation=True, **{field: "конец"})]), previous
    )
    assert not items and carry.pending
    assert warnings[0].code == "carryover_conflict" and field in warnings[0].message
    assert getattr(warnings[0].row, field) == "конец"
    assert getattr(carry.pending, field) == "начало"


async def test_legacy_snapshot_exports_and_sse_without_rewriting_history(root, client, pdf_bytes):
    """Старые DB payload и item-события читаются с новыми пустыми полями без SQL-миграции."""
    identifier = (await create(client, pdf_bytes, pages="1")).json()["job_id"]
    await root.processor.execute(identifier)
    with Session(root.repository.engine) as db, db.begin():
        record = db.get(JobRecord, identifier)
        payload = json.loads(json.dumps(record.payload))
        for item in payload["pages"][0]["items"]:
            for field in FIELDS[5:]:
                item["item"].pop(field, None)
        record.payload = payload
        event = db.scalar(
            select(EventRecord).where(
                EventRecord.job_id == identifier,
                EventRecord.payload["type"].as_string() == "item_validated",
            )
        )
        event_payload = json.loads(json.dumps(event.payload))
        for field in FIELDS[5:]:
            event_payload["data"]["item"].pop(field, None)
        event.payload = event_payload
        original_id, original_payload = event.id, event_payload
    exported = (
        await client.get(f"/api/v1/extractions/{identifier}/result.json", headers=HEADERS)
    ).json()
    snapshot = (
        await client.get(f"/api/v1/extractions/{identifier}/snapshot", headers=HEADERS)
    ).json()
    assert snapshot["partial"] == exported
    assert list(exported["items"][0]) == FIELDS
    assert all(exported["items"][0][field] == "" for field in FIELDS[5:])
    stream = await client.get(f"/api/v1/extractions/{identifier}/events", headers=HEADERS)
    blocks = [block for block in stream.text.split("\n\n") if "event: item_validated\n" in block]
    assert len(blocks) == 1
    data = json.loads(
        next(line[6:] for line in blocks[0].splitlines() if line.startswith("data: "))
    )
    assert data["item"] == exported["items"][0]
    with Session(root.repository.engine) as db:
        event = db.scalar(
            select(EventRecord).where(
                EventRecord.job_id == identifier, EventRecord.id == original_id
            )
        )
        assert event.payload == original_payload


def test_public_event_serialization_keeps_raw_model_stream():
    """Совместимость item-событий не преобразует исходный текст модели и метаданные."""
    event = Event(id=1, type="text_delta", data={"text": '{"rows": [', "revision": 2})
    assert event.public_data() == event.data


@pytest.mark.parametrize("headers", [HEADERS, {"Authorization": "Bearer " + HEADERS["X-API-Key"]}])
async def test_new_fields_reach_all_public_results_after_carryover(
    root, client, pdf_bytes, headers
):
    """Оба прежних API-credential передают новые графы через worker, SSE, snapshot и Excel."""
    from io import BytesIO

    from openpyxl import load_workbook

    root.model.responses = [
        page(
            [
                row(
                    caption="Клапан",
                    designation="DN100",
                    manufacturer="Ридан",
                    continues_next=True,
                    brand="Ридан",
                    article="00042/AB",
                    note="К1",
                )
            ],
            article_header="Артикул",
        ).model_dump_json(),
        page([row(caption="", continuation=True)]).model_dump_json(),
    ]
    identifier = (await create(client, pdf_bytes)).json()["job_id"]
    await root.processor.execute(identifier)
    prefix = f"/api/v1/extractions/{identifier}"
    result = (await client.get(prefix + "/result.json", headers=headers)).json()
    expected = {
        "Caption": "Клапан DN100 Ридан",
        "Units": "шт",
        "Quantity": "2,5",
        "UserComment1": "TEST-PROJECT",
        "Pages": "1",
        "Brand": "Ридан",
        "Article": "00042/AB",
        "Note": "К1",
    }
    assert result == {"items": [expected]}
    snapshot = (await client.get(prefix + "/snapshot", headers=headers)).json()
    assert snapshot["partial"] == result
    stream = (await client.get(prefix + "/events", headers=headers)).text
    blocks = [block for block in stream.split("\n\n") if "event: item_validated\n" in block]
    event = json.loads(
        next(line[6:] for line in blocks[0].splitlines() if line.startswith("data: "))
    )
    assert event["item"] == expected
    assert (
        root.model.calls[1]["context"]["previous_adjacent_page"]["pending"]["article"] == "00042/AB"
    )
    excel = await client.get(prefix + "/result.xlsx", headers=headers)
    sheet = load_workbook(BytesIO(excel.content)).active
    assert sheet["M2"].value == expected["Brand"] and sheet["N2"].value == "00042/AB"
    assert sheet["R2"].value == expected["Note"] and sheet["Q2"].value == "1"


@pytest.mark.parametrize(
    "header", ["", "Код оборудования, изделия, материала", "Марка", "НеАртикул"]
)
def test_catalogue_code_is_not_an_article_without_literal_header(header):
    """Регрессия реального ТМ: каталожный код из другой графы остаётся только в диагностике."""
    items, warnings, _ = reconcile(
        1, "P", page([row(article="065N0366GR")], article_header=header), None
    )
    assert len(items) == 1 and items[0].item.Article == ""
    assert warnings[0].code == "article_column_unverified"
    assert warnings[0].row.article == "065N0366GR"


@pytest.mark.parametrize("header", ["Артикул", "Арти-\nкул", "Артикул производителя"])
def test_article_requires_the_literal_source_column(header):
    """Заголовок разрешает артикул с ведущими нулями, включая перенос названия графы."""
    items, warnings, _ = reconcile(
        1, "P", page([row(article="00042/AB")], article_header=header), None
    )
    assert not warnings and items[0].item.Article == "00042/AB"


async def test_v2_prompt_keeps_v1_rules_and_existing_job_snapshot(root, client, pdf_bytes):
    """Новая версия дополняет прежние инструкции и не заменяет prompt уже созданного задания."""
    old_path = Path("prompts/extract_specification_v1.txt")
    old = await asyncio.to_thread(old_path.read_text, encoding="utf8")
    new = root.extractions.default_prompt
    assert new.startswith(old.strip())
    root.extractions.default_prompt = old
    identifier = (await create(client, pdf_bytes, pages="1")).json()["job_id"]
    original = await root.repository.get(identifier)
    root.extractions.default_prompt = new
    await root.processor.execute(identifier)
    saved = await root.repository.get(identifier)
    assert root.model.calls[0]["prompt"] == old
    assert saved.prompt == old and saved.prompt_version == original.prompt_version


def test_vlm_warning_length_is_bounded_without_changing_saved_diagnostics():
    """Регрессия зацикливания ТМ-9: схема ограничивает новый текст, но читает старую диагностику."""
    accepted = page([])
    accepted.warnings = ["x" * 600]
    assert parse_model_response(accepted.model_dump_json()).warnings == ["x" * 600]
    rejected = json.loads(accepted.model_dump_json())
    rejected["warnings"] = ["x" * 601]
    with pytest.raises(ValidationError):
        type(accepted).model_validate(rejected)
    assert len(Warning(pdf_page=9, code="model_warning", message="x" * 40000).message) == 40000
