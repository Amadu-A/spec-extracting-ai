# tests/test_excel_template.py
"""Регрессия 18-колоночного шаблона, сохранности значений и безопасности XLSX."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from app.core.settings import Settings
from app.domain.models import Export, SpecificationItem, Warning
from app.infrastructure.excel import TEMPLATE_PATH, build_excel

MAPPING = {
    "C": "Caption",
    "D": "Units",
    "E": "Quantity",
    "M": "Brand",
    "N": "Article",
    "P": "UserComment1",
    "Q": "Pages",
    "R": "Note",
}
HEADERS = [
    "Номер по схеме",
    "Литера по схеме",
    "Номенклатура",
    "Ед. изм.",
    "Кол-во",
    "Цена (договорная)",
    "Валюта",
    "% скидки",
    "Цена закупки",
    "Дата цены",
    "Цена (розничная)",
    "Сумма",
    "Бренд",
    "Артикул",
    "Раздел",
    "Шифр документа",
    "Страница документа",
    "Примечание",
]


def item(**changes) -> SpecificationItem:
    """Задаёт независимые от настоящих документов данные со значимыми строковыми форматами."""
    return SpecificationItem.model_validate(
        {
            "Caption": "Клапан Ридан",
            "Units": "шт",
            "Quantity": "001,50",
            "UserComment1": "P",
            "Pages": "3",
            "Brand": "Ридан",
            "Article": "0001/02-AB",
            "Note": "Для ИТП",
            **changes,
        }
    )


@pytest.mark.parametrize("count", [0, 1, 45, 46, 47, 100])
def test_template_row_counts_values_and_styles(count):
    """Количество строк точно равно заданию; исходные стили и фиксированная валюта сохранены."""
    source = load_workbook(TEMPLATE_PATH)
    result = Export(items=[item(Caption=f"Позиция {index}") for index in range(count)])
    data = build_excel(result, [Warning(pdf_page=1, code="test", message="Проверка")])
    book = load_workbook(BytesIO(data))
    assert book.sheetnames == ["Лист_1"]
    sheet, template = book.active, source.active
    assert sheet.max_column == 18 and sheet.max_row == count + 1
    assert [cell.value for cell in sheet[1]] == HEADERS
    assert {key: dict(value) for key, value in sheet.column_dimensions.items()} == {
        key: dict(value) for key, value in template.column_dimensions.items()
    }
    assert sheet.sheet_format == template.sheet_format
    for index, row in enumerate(result.items, start=2):
        original = min(index, 47)
        assert sheet.row_dimensions[index].height == template.row_dimensions[original].height
        for cell in sheet[index]:
            assert cell._style == template.cell(original, cell.column)._style
            if cell.column_letter in MAPPING:
                assert cell.value == getattr(row, MAPPING[cell.column_letter])
                assert cell.data_type == "s"
            else:
                assert cell.value == ("RUB" if cell.column_letter == "G" else None)
    assert not any(index > count + 1 for index in sheet.row_dimensions)


def test_packaged_template_contains_no_product_values():
    """Поставляемый ресурс не содержит ни одного демонстрационного товара или формулы."""
    book = load_workbook(TEMPLATE_PATH)
    assert book.sheetnames == ["Лист_1"]
    assert book.active.max_row == 47
    for row in book.active.iter_rows(min_row=2):
        for cell in row:
            assert cell.value == ("RUB" if cell.column == 7 else None)
    with ZipFile(TEMPLATE_PATH) as archive:
        assert not any(
            "externalLink" in name or "vbaProject" in name for name in archive.namelist()
        )


@pytest.mark.parametrize(
    "field", ["Caption", "Units", "Quantity", "UserComment1", "Brand", "Article", "Note"]
)
@pytest.mark.parametrize(
    "value", ['=HYPERLINK("https://evil.invalid")', "+1+1", "-2+1", "@SUM(1,2)"]
)
def test_all_untrusted_fields_remain_literal_text(field, value):
    """Формулы в любом доступном текстовом поле не исполняются и не получают апостроф."""
    result = Export(items=[item(**{field: value})])
    data = build_excel(result, [])
    book = load_workbook(BytesIO(data), data_only=False)
    column = next(column for column, name in MAPPING.items() if name == field)
    assert book.active[f"{column}2"].value == value
    assert book.active[f"{column}2"].data_type == "s"
    with ZipFile(BytesIO(data)) as archive:
        assert b"<f>" not in archive.read("xl/worksheets/sheet1.xml")


def test_empty_optionals_and_concurrent_exports_are_independent():
    """Пустые поля не берутся из шаблона/соседнего запроса, общий ресурс не мутирует."""
    before = TEMPLATE_PATH.read_bytes()
    exports = [
        Export(items=[item(Caption=f"Запрос {i}", Brand="", Article="", Note="")]) for i in range(8)
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda result: build_excel(result, []), exports))
    for index, data in enumerate(results):
        sheet = load_workbook(BytesIO(data)).active
        assert sheet["C2"].value == f"Запрос {index}"
        assert all(sheet[f"{column}2"].value is None for column in ("M", "N", "R"))
    assert TEMPLATE_PATH.read_bytes() == before


@pytest.mark.corpus
def test_template_matches_supplied_workbook():
    """Подготовленный ресурс сохраняет все стили, геометрию и настройки фактического исходника."""
    path = Path("examples/шаблон.xlsx")
    if not path.exists():
        pytest.skip("Локальный шаблон examples не предоставлен")
    source, target = load_workbook(path), load_workbook(TEMPLATE_PATH)
    left, right = source.active, target.active
    assert source.loaded_theme == target.loaded_theme
    for original, prepared in (
        (left.column_dimensions, right.column_dimensions),
        (left.row_dimensions, right.row_dimensions),
    ):
        assert {key: dict(value) for key, value in original.items()} == {
            key: dict(value) for key, value in prepared.items()
        }
    assert left.sheet_format == right.sheet_format
    assert left.sheet_view == right.sheet_view
    assert left.page_setup == right.page_setup and left.page_margins == right.page_margins
    for old_row, new_row in zip(left, right, strict=True):
        for old, new in zip(old_row, new_row, strict=True):
            assert old._style == new._style
            if old.row == 1 or old.column == 7:
                assert old.value == new.value


def test_export_at_default_item_limit():
    """20 000 позиций проходят весь XLSX-путь; последняя строка сохраняет точные значения."""
    count = Settings(_env_file=None).max_items
    result = Export(items=[item(Caption=f"Граничная позиция {index}") for index in range(count)])
    data = build_excel(result, [])
    book = load_workbook(BytesIO(data), read_only=True)
    sheet = book.active
    assert sheet.max_row == count + 1 and sheet.max_column == 18
    last = next(sheet.iter_rows(min_row=count + 1, max_row=count + 1, values_only=True))
    assert last[2] == f"Граничная позиция {count - 1}"
    assert last[4] == "001,50" and last[13] == "0001/02-AB"
