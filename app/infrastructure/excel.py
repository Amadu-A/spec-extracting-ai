# app/infrastructure/excel.py
"""Адаптер SpreadsheetExporter: копия утверждённого шаблона без демоданных.

Добавляет источник между разделом и шифром документа, заполняет восемь полей
позиции и серверное имя PDF. Предупреждения остаются в API диагностики.
"""

import asyncio
from copy import copy
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.domain.models import Export, Warning

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "resources/specification_v1.xlsx"
ITEM_COLUMNS = {
    "Caption": "C",
    "Units": "D",
    "Quantity": "E",
    "UserComment1": "Q",
    "Pages": "R",
    "Brand": "M",
    "Article": "N",
    "Note": "S",
}


def add_source_column(sheet: Worksheet) -> None:
    """Расширяет копию исходного шаблона, сохраняя стили и размеры прежних граф."""
    sheet.insert_cols(16)
    for column in range(18, 15, -1):
        target = get_column_letter(column + 1)
        dimension = copy(sheet.column_dimensions[get_column_letter(column)])
        dimension.index = target
        dimension.min = dimension.max = column + 1
        sheet.column_dimensions[target] = dimension
    sheet.column_dimensions["P"].width = 30
    for index in range(1, sheet.max_row + 1):
        sheet[f"P{index}"]._style = copy(sheet[f"Q{index}"]._style)
    sheet["P1"] = "Источник/Наименование файла"


class ExcelExporter:
    """Выполняет чтение ресурса и CPU/ZIP операции openpyxl вне HTTP event loop."""

    async def excel(self, result: Export, warnings: list[Warning]) -> bytes:
        """Реализует прежний application-порт без зависимости от HTTP и конвертеров."""
        return await asyncio.to_thread(build_excel, result, warnings)


def build_excel(result: Export, warnings: list[Warning]) -> bytes:
    """Заполняет текущие позиции, сохраняя оформление и строковые значения JSON.

    XML-тип строки запрещает исполнение формул и преобразование в даты/числа.
    Числовые форматы шаблона сохраняются. warnings принимаются для совместимости
    порта, без дополнительного листа; они доступны через API диагностики.
    """
    book = load_workbook(TEMPLATE_PATH)
    sheet = book["Лист_1"]
    add_source_column(sheet)
    sample_row = sheet.max_row
    sample_cells = [(cell.column, copy(cell._style), cell.value) for cell in sheet[sample_row]]
    sample_dimension = copy(sheet.row_dimensions[sample_row])
    for index, item in enumerate(result.items, start=2):
        if index > sample_row:
            dimension = copy(sample_dimension)
            dimension.index = index
            sheet.row_dimensions[index] = dimension
            for column, style, value in sample_cells:
                target = sheet.cell(index, column)
                target._style = copy(style)
                target.value = value
        for field, column in ITEM_COLUMNS.items():
            cell = sheet[f"{column}{index}"]
            cell.value = getattr(item, field)
            cell.data_type = "s"
        source = sheet[f"P{index}"]
        source.value = result.file_name
        source.data_type = "s"
    last_row = len(result.items) + 1
    if last_row < sample_row:
        sheet.delete_rows(last_row + 1, sample_row - last_row)
        for index in list(sheet.row_dimensions):
            if index > last_row:
                del sheet.row_dimensions[index]
    output = BytesIO()
    book.save(output)
    book.close()
    return output.getvalue()
