# app/domain/invoices/evidence.py
"""Сверка с физическими графами PDF; не вычисляет и не подгоняет суммы строк."""

import re
from decimal import Decimal, InvalidOperation

from app.domain.invoices.models import InvoiceItem, InvoicePage, decimal_text


def cell_text(value: str | None) -> str:
    """Сохраняет печатные символы; перенос после дефиса кода не создаёт новый пробел."""
    value = (value or "").replace("-\n", "-")
    value = re.sub(r"(?<=[xх])\n(?=\d)", "", value)
    value = re.sub(r"(?<=\d)\n(?=[.,]\d)", "", value)
    value = re.sub(r"(\d+[.,]\d*)\n(?=\d)", r"\1", value)
    return " ".join(value.split())


def source_caption(source: str, model: str) -> str:
    """Сохраняет все печатные символы графы, используя vision только для пробелов внутри переносимых слов."""
    compact = re.sub(r"\s", "", source)
    reading = re.sub(r"\s", "", model)
    if compact == reading or compact.replace("_", "") == reading.replace("_", ""):
        return model
    if reading and compact.startswith(reading):
        consumed = 0
        for index, char in enumerate(source):
            consumed += not char.isspace()
            if consumed == len(reading):
                return model.rstrip() + " " + source[index + 1 :].lstrip()
    return source


def header_text(value: str | None) -> str:
    """Сопоставляет названия граф независимо от разбиения заголовка по строкам."""
    return re.sub(r"[^а-яa-z0-9]", "", (value or "").casefold().replace("ё", "е"))


def mixed_code(item: InvoiceItem) -> str | None:
    """Не выдаёт имя производителя из смешанной графы за подтверждённый артикул."""
    raw = item.product_reference_raw
    if not raw or raw in {item.brand, item.manufacturer} or not re.fullmatch(r"[\w./ -]+", raw):
        return None
    digits = sum(char.isdigit() for char in raw)
    code_words = re.fullmatch(r"[a-z]+(?:[-_/][a-z]+)+", raw)
    return raw if digits >= 2 or (digits and re.search(r"[./-]", raw)) or code_words else None


def financial_tables(tables: list[dict]) -> list[tuple[dict, list[list]]]:
    """Выбирает товарные таблицы по заголовкам, а не имени файла/поставщику."""
    result = []
    for table in tables:
        rows = table.get("rows", [])
        for offset, row in enumerate(rows[:3]):
            headers = [header_text(value) for value in row]
            caption = next(
                (
                    i
                    for i, h in enumerate(headers)
                    if h
                    in (
                        "товар",
                        "товарыработыуслуги",
                        "наименованиетоварауслуги",
                        "наименование",
                        "номенклатура",
                    )
                    or h.startswith("наименованиетовара")
                ),
                None,
            )
            if caption is None:
                continue
            fields = {"caption": caption}
            for index, h in enumerate(headers):
                if h in ("количество", "колво"):
                    fields["quantity"] = index
                elif h in ("ед", "едизм", "единицаизмерения"):
                    fields["units"] = index
                elif h in ("код", "кодтовараэтм"):
                    fields["supplier_code"] = index
                elif h.startswith("артикул"):
                    fields["product_reference_raw" if "произв" in h else "article"] = index
                elif h in ("ценабезндс", "ценаруб"):
                    fields["unit_price_without_vat"] = index
                elif h in ("ценавклндс", "ценасндс"):
                    fields["unit_price_with_vat"] = index
                elif (
                    h in ("суммабезндс", "стоимостьбезндс")
                    or (h.startswith("сумма") and h.endswith("безндс"))
                    or (h == "суммаруб" and "ндс" in headers)
                ):
                    fields["line_total_without_vat"] = index
                elif h in ("всегосндс", "всегосндсруб", "стоимостьсндс", "суммасндс") or (
                    h.startswith("всего") and h.endswith("сндс")
                ):
                    fields["line_total_with_vat"] = index
                elif h in ("суммандс", "суммандсруб"):
                    fields["vat_amount"] = index
                elif h in ("ндс", "ставкандс"):
                    fields["vat_rate"] = index
            if "quantity" in fields and any(
                field.startswith("unit_price") or field.startswith("line_total") for field in fields
            ):
                result.append((fields, rows[offset + 1 :]))
            break
    return result


def native_rows(tables: list[dict]) -> dict[int, list[dict]]:
    """Адресует строки их напечатанным номером; неоднозначный повтор не разрешается угадыванием."""
    output = {}
    for fields, rows in financial_tables(tables):
        for row in rows:
            if not row or not re.fullmatch(r"\d+\*?", cell_text(row[0])):
                continue
            # Строка номеров граф 1..14 не является товарной позицией.
            if all(cell_text(value) == str(i + 1) for i, value in enumerate(row)):
                continue
            number = int(cell_text(row[0]).rstrip("*"))
            if number < 1:
                continue
            values = {
                key: cell_text(row[index]) or None
                for key, index in fields.items()
                if index < len(row)
            }
            if values.get("caption"):
                output.setdefault(number, []).append(values)
    return output


def apply_native(item: InvoiceItem, evidence: list[dict], notes: list[str]) -> None:
    """Предпочитает дословные ячейки текста PDF; числовое противоречие явно предупреждается."""
    if len(evidence) != 1:
        if evidence:
            notes.append(
                f"Строка {item.row_number}: несколько исходных таблиц с тем же номером; требуется сверка"
            )
        return
    row = evidence[0]
    for field in (
        "unit_price_without_vat",
        "unit_price_with_vat",
        "line_total_without_vat",
        "vat_amount",
        "line_total_with_vat",
        "vat_rate",
    ):
        if field not in row:
            setattr(item, field, None)
            item.field_origin.pop(field, None)
            if field == "vat_rate":
                item.vat_rate_source = None
    if "supplier_code" not in row:
        item.supplier_code = None
    if "product_reference_raw" not in row:
        item.product_reference_raw = None
    inline = re.search(r"\(\s*арт(?:икул)?[.:\s]*([^)]+)\)", row.get("caption") or "", re.I)
    if inline:
        item.article = inline.group(1).strip()
        row = {
            **row,
            "caption": " ".join(
                re.sub(r"\(\s*арт(?:икул)?[.:\s]*[^)]+\)", "", row["caption"], flags=re.I).split()
            ),
        }
    elif "article" not in row and "product_reference_raw" not in row and item.article:
        item.article_candidate = item.article_candidate or item.article
        item.article = None
    for key, value in row.items():
        if key == "caption" and value is not None:
            value = source_caption(value, item.caption)
        if (
            key == "product_reference_raw"
            and value
            and getattr(item, key)
            and not any(c.isdigit() for c in value)
        ):
            if re.sub(r"\s", "", value) == re.sub(r"\s", "", getattr(item, key)):
                value = getattr(item, key)
        if key in {
            "quantity",
            "unit_price_without_vat",
            "unit_price_with_vat",
            "line_total_without_vat",
            "vat_amount",
            "line_total_with_vat",
            "vat_rate",
        }:
            try:
                value = decimal_text(value.rstrip("%") if value is not None else None)
            except ValueError:
                notes.append(
                    f"Строка {item.row_number}: не удалось подтвердить числовую графу {key}"
                )
                continue
            old = getattr(item, key)
            if old is not None and value is not None:
                try:
                    if Decimal(old) != Decimal(value):
                        notes.append(
                            f"Строка {item.row_number}: ответ модели противоречит ячейке PDF в {key}; сохранено значение исходной графы"
                        )
                except InvalidOperation:
                    continue
        setattr(item, key, value)
        item.field_origin[key] = "source"
    raw = item.product_reference_raw
    if raw:
        item.article = mixed_code(item)
    elif "article" not in row and inline is None and item.row_kind == "product":
        # Отдельной графы нет: хвост наименования остаётся только неподтверждённым кандидатом.
        tail = item.caption.rsplit(None, 1)[-1]
        if (
            len(tail) <= 100
            and re.fullmatch(r"[\w./-]+", tail)
            and any(char.isdigit() for char in tail)
        ):
            item.article_candidate = tail
            item.article_candidate_source = "caption_suffix"
    if item.vat_rate is not None and "vat_rate" in row:
        item.vat_rate_source = "row"


def attachment_heading(text: str) -> bool:
    """Только самостоятельный заголовок соглашения, не ссылка на договор внутри счёта."""
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return bool(
        re.match(
            r"(?:соглашение\s+на\s+поставку|договор\s+поставки|техническ(?:ие|ая)\s+(?:характеристики|спецификация))\b",
            first,
            re.I,
        )
    )


def document_heading(text: str) -> dict | None:
    """Отличает самостоятельный заголовок спецификации от ссылки на неё в тексте счёта."""
    matches = set(re.findall(r"^\s*Спецификация\s*[№N]\s*([^\s,]+)", text[:2000], re.I | re.M))
    if len(matches) != 1:
        return None
    offer = re.search(r"ТКП\s*№\s*([^\s,]+)", text[:500], re.I)
    return dict(
        document_type="specification",
        number=next(iter(matches)),
        related_commercial_offer_number=offer.group(1) if offer else None,
    )


def needs_financial_review(page: InvoicePage) -> bool:
    """У платёжной строки без количества уточняет налоговую трактовку общих Цена/Сумма."""
    return len(page.documents) == 1 and any(
        row.item.row_kind in ("final_payment", "advance_payment")
        and row.item.quantity is None
        and row.item.units is None
        for row in page.documents[0].rows
    )


def included_discounts(text: str) -> list[str]:
    """Читает сумму уже предоставленной скидки из явной пометки, без сравнения итогов."""
    pattern = (
        r"^\s*(?:вам\s+)?предоставлен(?:а|ы)?\s+скидк[а-я]*\s+"
        r"(?:\d+(?:[.,]\d+)?\s*%\s+)?на\s+"
        r"([0-9][0-9 \u00a0\u202f]*(?:[.,][0-9]+)?)\s*руб"
    )
    values = [decimal_text(value) for value in re.findall(pattern, text, re.I | re.M)]
    return [value for value in values if re.fullmatch(r"\d{1,24}(?:\.\d{1,12})?", value)]
