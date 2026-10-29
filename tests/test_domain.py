# tests/test_domain.py
"""Чистые правила диапазонов, экспортной схемы, переносов и происхождения строк."""

import json

import pytest
from pydantic import ValidationError

from app.domain.errors import ServiceError
from app.domain.models import Carryover, Export, PageExtraction, Row, SpecificationItem
from app.domain.pages import parse_pages
from app.domain.reconcile import parse_model_response, reconcile


@pytest.mark.parametrize(
    ("expression", "expected"), [("1-3, 2, 5", [1, 2, 3, 5]), (" 5 , 1-2 ", [1, 2, 5]), ("1", [1])]
)
def test_ranges(expression, expected):
    """Диапазоны сортируются, физические номера начинаются с 1, дубликаты удаляются."""
    assert parse_pages(expression, 5, 10) == expected


@pytest.mark.parametrize(
    "expression",
    ["", " ", "0", "-1", "3-2", "1,,2", "1-6", "1-999999", "1;2", "1.5", "a", "1-", ",1"],
)
def test_invalid_ranges(expression):
    """Невалидный диапазон отклоняется до разворачивания большого списка."""
    with pytest.raises(ServiceError):
        parse_pages(expression, 5, 5)


def test_range_limits():
    """Лимит учитывает объединение отдельных диапазонов, а не только каждый токен."""
    with pytest.raises(ServiceError, match="не более"):
        parse_pages("1-3,4-6", 10, 5)


@pytest.mark.parametrize("quantity", ["2,50", "2.50", "001", "0", "1/2", "2×3"])
def test_quantity_string_is_preserved(quantity):
    """Экспорт не выполняет арифметику и не заменяет исходное представление числа."""
    item = SpecificationItem(
        Caption="Клапан", Units="шт.", Quantity=quantity, UserComment1="Проект", Pages="7"
    )
    result = Export(items=[item]).model_dump()
    assert list(result) == ["items"]
    assert list(result["items"][0]) == ["Caption", "Units", "Quantity", "UserComment1", "Pages"]
    assert result["items"][0]["Quantity"] == quantity


@pytest.mark.parametrize(
    "update", [{"Caption": ""}, {"Quantity": 2}, {"Pages": "0"}, {"Units": ""}, {"extra": "bad"}]
)
def test_strict_schema(update):
    """Лишние поля, нестроковые и критически пустые значения не проходят схему."""
    data = {"Caption": "Клапан", "Units": "шт", "Quantity": "2", "UserComment1": "A", "Pages": "1"}
    with pytest.raises(ValidationError):
        SpecificationItem.model_validate({**data, **update})


def row(**update) -> Row:
    """Строит явную VLM строку для проверки чистых правил слияния."""
    return Row.model_validate(
        {
            "row_id": "table1/1",
            "designation": "",
            "manufacturer": "",
            "caption": "Клапан DN100 Завод",
            "units": "шт",
            "quantity": "2,5",
            "continuation": False,
            "continues_next": False,
            "verified": True,
            **update,
        }
    )


def page(rows, **update) -> PageExtraction:
    """Задаёт внутренний ответ модели без экспортных Pages/UserComment1."""
    return PageExtraction(
        rows=rows, group_context=["Арматура"], warnings=[], completeness="complete", **update
    )


def test_real_duplicates_and_replayed_fragment():
    """Равные товары сохраняются; коллизия row_id отклоняется без тихого удаления строки."""
    items, warnings, _ = reconcile(4, "P", page([row(), row(row_id="table1/2")]), None)
    assert len(items) == 2
    assert [e.item.Pages for e in items] == ["4", "4"]
    assert not warnings
    with pytest.raises(ValidationError):
        page([row(), row()])


def test_caption_is_composed_from_explicit_source_columns():
    """Марка и изготовитель не зависят от произвольной пунктуации модели в caption."""
    items, warnings, _ = reconcile(
        1, "P", page([row(caption="Клапан", designation="DN100", manufacturer="Ридан")]), None
    )
    assert not warnings and items[0].item.Caption == "Клапан DN100 Ридан"


def test_diagnostic_repetition_does_not_inflate_problem_count():
    """Одинаковые model remarks одной страницы объединяются, одинаковые товары остаются разными."""
    result = page([row(), row(row_id="2")])
    result.warnings = [
        "Проверьте штамп",
        "Проверьте штамп",
        " Другая причина ",
        "Другая причина",
        " ",
    ]
    items, warnings, _ = reconcile(1, "P", result, None)
    assert len(items) == 2
    assert [warning.message for warning in warnings] == ["Проверьте штамп", "Другая причина"]


def test_adjacent_carryover_and_group_context():
    """Продолжение собирает многострочное имя и сохраняет PDF-страницу начала позиции."""
    first = reconcile(
        2, "P", page([row(caption="Клапан", units="", quantity="", continues_next=True)]), None
    )
    assert not first[0] and first[2].pending
    items, warnings, carry = reconcile(
        3, "P", page([row(caption="DN100 Завод", continuation=True)]), first[2]
    )
    assert not warnings and not carry.pending
    assert items[0].item.Caption == "Клапан DN100 Завод"
    assert items[0].source_pages == [2, 3] and items[0].item.Pages == "2"
    assert carry.group_context == ["Арматура"]


def test_carryover_never_crosses_gap():
    """Выбранные листы 2 и 4 не считаются соседними, продолжение не выдумывается."""
    previous = Carryover(pdf_page=2, pending=row(continues_next=True), source_pages=[2])
    items, warnings, _ = reconcile(4, "P", page([row(continuation=True)]), previous)
    assert not items
    assert {w.code for w in warnings} == {"carryover_gap", "orphan_continuation"}


def test_conflict_and_unverified_rows():
    """Несогласованные количества и отсутствующие критические поля остаются diagnostics."""
    previous = Carryover(pdf_page=1, pending=row(continues_next=True), source_pages=[1])
    items, warnings, _ = reconcile(2, "P", page([row(continuation=True, quantity="3")]), previous)
    assert not items and warnings[0].code == "carryover_conflict"
    items, warnings, _ = reconcile(
        1, "P", page([row(quantity=""), row(row_id="2", verified=False)]), None
    )
    assert not items and all(w.code == "unverified_row" for w in warnings)


def test_parse_only_safe_fences():
    """Принимается внешняя ограда, но не обрезанный JSON и не произвольный префикс."""
    valid = page([]).model_dump_json()
    assert parse_model_response("```json\n" + valid + "\n```").rows == []
    for invalid in [valid[:-1], "Текст " + valid, json.dumps({"items": []}), valid + " text"]:
        with pytest.raises(ServiceError) as error:
            parse_model_response(invalid)
        assert error.value.code == "invalid_model_json"
