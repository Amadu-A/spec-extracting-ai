# app/domain/invoices/validation.py
"""Проверяет опубликованную арифметику через Decimal, не исправляя источник."""

from decimal import Decimal, localcontext

from app.domain.invoices.models import Check, InvoiceDocument, Validation


def compare(name, calculated, printed, tolerance=Decimal("0.01"), row=None) -> Check:
    """Отсутствующее значение не становится нулём или успешной проверкой."""
    if calculated is None or printed is None:
        return Check(check=name, status="not_checkable", row_number=row)
    actual = Decimal(printed)
    difference = calculated - actual
    return Check(
        check=name,
        status="passed" if abs(difference) <= tolerance else "failed",
        calculated=format(calculated, "f"),
        document_value=printed,
        difference=format(difference, "f"),
        tolerance=format(tolerance, "f"),
        row_number=row,
    )


def validate_document(document: InvoiceDocument) -> Validation:
    """Сверяет строки/итоги, точность цены и отдельные неучтённые компоненты."""
    with localcontext() as context:
        context.prec = 64
        checks = []
        warnings = list(document.notes)
        for field, total, name in (
            ("line_total_without_vat", "without_vat", "sum_line_totals_without_vat"),
            ("vat_amount", "vat", "sum_line_vat"),
            ("line_total_with_vat", "with_vat", "sum_line_totals_with_vat"),
        ):
            values = [getattr(item, field) for item in document.items]
            components = [
                getattr(a, total)
                for a in document.totals.adjustments
                if not a.included_in_line_totals
            ]
            complete = bool(values) and all(v is not None for v in [*values, *components])
            calculated = (
                sum((Decimal(v) for v in [*values, *components]), Decimal(0)) if complete else None
            )
            checks.append(compare(name, calculated, getattr(document.totals, total)))
        totals = document.totals
        net_vat = (
            Decimal(totals.without_vat) + Decimal(totals.vat)
            if totals.without_vat is not None and totals.vat is not None
            else None
        )
        checks.append(compare("document_tax_arithmetic", net_vat, totals.with_vat))
        for item in document.items:
            for price_name, line_name in (
                ("unit_price_without_vat", "line_total_without_vat"),
                ("unit_price_with_vat", "line_total_with_vat"),
            ):
                price, line = getattr(item, price_name), getattr(item, line_name)
                # Неприменимая пара колонок не является обязательной проверкой.
                if price is None:
                    continue
                product, tolerance = None, Decimal("0.01")
                if price is not None and item.quantity is not None:
                    quantity = Decimal(item.quantity)
                    value = Decimal(price)
                    product = quantity * value
                    tolerance += Decimal(5).scaleb(value.as_tuple().exponent - 1) * abs(quantity)
                checks.append(
                    compare("quantity_x_" + price_name, product, line, tolerance, item.row_number)
                )
            if item.row_kind in ("advance_payment", "final_payment") or item.requires_manual_review:
                warnings.append(f"Строка {item.row_number}: платёж/позиция требует ручной проверки")
            if item.article_candidate and not item.article:
                warnings.append(
                    f"Строка {item.row_number}: кандидат в артикул не подтверждён отдельной графой/пометкой"
                )
        if document.document_type == "commercial_offer":
            warnings.append(
                "Коммерческое предложение не является автоматически основанием для оплаты"
            )
        if document.document_type == "unknown":
            warnings.append("Не удалось однозначно определить вид документа")
        statuses = {c.status for c in checks}
        status = (
            "failed"
            if "failed" in statuses
            else (
                "not_checkable"
                if statuses == {"not_checkable"}
                else "partially_checked"
                if "not_checkable" in statuses
                else "passed"
            )
        )
        if status != "passed":
            warnings.append(
                {
                    "failed": "Доказано расхождение сумм; значения источника сохранены",
                    "partially_checked": "Часть проверок невозможна: исходные поля отсутствуют",
                    "not_checkable": "Недостаточно исходных чисел для проверки",
                }[status]
            )
        return Validation(status=status, checks=checks, warnings=list(dict.fromkeys(warnings)))
