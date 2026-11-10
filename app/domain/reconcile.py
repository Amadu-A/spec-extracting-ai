# app/domain/reconcile.py
"""Консервативное согласование строк: переносы только между соседними PDF-листами."""

import json
import re

from pydantic import ValidationError

from app.domain.errors import ServiceError
from app.domain.models import Carryover, Evidence, PageExtraction, Row, SpecificationItem, Warning


def parse_model_response(raw: str) -> PageExtraction:
    """Принимает полный JSON; безопасно снимает только внешнюю Markdown-ограду."""
    text = raw.strip()
    if text.startswith("```json\n") and text.endswith("```"):
        text = text[8:-3].strip()
    elif text.startswith("```\n") and text.endswith("```"):
        text = text[4:-3].strip()
    try:
        return PageExtraction.model_validate(json.loads(text))
    except (ValueError, ValidationError) as exc:
        raise ServiceError(
            "invalid_model_json", "Модель вернула ответ вне внутренней JSON-схемы", 502
        ) from exc


def reconcile(
    page: int, project: str, result: PageExtraction, previous: Carryover | None
) -> tuple[list[Evidence], list[Warning], Carryover]:
    """Сохраняет реальные повторы и исключает неподтверждённые строки из экспорта.

    Незавершённая строка остаётся carryover до доказанного continuation.
    Номер Pages и код проекта устанавливает только backend.
    """
    items: list[Evidence] = []
    warnings = [
        Warning(pdf_page=page, code="model_warning", message=message)
        for message in dict.fromkeys(w.strip() for w in result.warnings if w.strip())
    ]
    if result.completeness == "uncertain":
        warnings.append(
            Warning(
                pdf_page=page,
                code="coverage_unverified",
                message="Модель не подтвердила полноту страницы",
            )
        )
    pending, source_pages = None, []
    if previous and previous.pending:
        if previous.pdf_page == page - 1:
            pending, source_pages = previous.pending, previous.source_pages
        else:
            warnings.append(
                Warning(
                    pdf_page=previous.pdf_page,
                    code="carryover_gap",
                    message="Перенос через пропущенные PDF-страницы запрещён",
                    row=previous.pending,
                )
            )
    pending_header = previous.article_header if previous and pending else ""
    seen: dict[str, Row] = {}
    for row in result.rows:
        if row.row_id in seen:
            raise ServiceError(
                "ambiguous_model_rows", "Повторный row_id требует повторной обработки страницы", 502
            )
        seen[row.row_id] = row
        article_header = result.article_header
        if row.continuation and previous and previous.pdf_page == page - 1:
            article_header = article_header or previous.article_header
        normalized_header = re.sub(r"[\-–­]\s*", "", article_header).casefold()
        if row.article and not re.search(
            r"\b(?:артикул(?:ы)?|код\s+оборудования\s*,?\s*изделия\s*,?\s*материала)\b",
            normalized_header,
        ):
            warnings.append(
                Warning(
                    pdf_page=page,
                    code="article_column_unverified",
                    message="Значение Article исключено: модель не подтвердила заголовок «Артикул» или «Код оборудования, изделия, материала». Исходное значение сохранено в строке диагностики.",
                    row=row,
                )
            )
            row = row.model_copy(update={"article": ""})
        origin = page
        sources = [page]
        if row.continuation:
            if not pending or not previous or previous.pdf_page != page - 1:
                warnings.append(
                    Warning(
                        pdf_page=page,
                        code="orphan_continuation",
                        message="Не найдена строка начала переноса",
                        row=row,
                    )
                )
                continue
            conflicts = []
            for field in ("units", "quantity", "brand", "article", "note"):
                before, after = getattr(pending, field), getattr(row, field)
                if field in ("brand", "article", "note"):
                    before, after = before.strip(), after.strip()
                if before and after and before != after:
                    conflicts.append(field)
            if conflicts:
                warnings.append(
                    Warning(
                        pdf_page=page,
                        code="carryover_conflict",
                        message="Поля продолжения противоречат исходной строке: "
                        + ", ".join(conflicts),
                        row=row,
                    )
                )
                continue
            sources = sorted(set(source_pages + [page]))
            origin = min(sources)
            row = row.model_copy(
                update={
                    "caption": " ".join(
                        filter(None, [pending.caption.strip(), row.caption.strip()])
                    ),
                    "units": pending.units or row.units,
                    "quantity": pending.quantity or row.quantity,
                    **{
                        field: getattr(pending, field).strip() or getattr(row, field).strip()
                        for field in ("brand", "article", "note")
                    },
                    "designation": pending.designation
                    if pending.designation == row.designation
                    else " ".join(filter(None, [pending.designation, row.designation])),
                    "manufacturer": pending.manufacturer
                    if pending.manufacturer == row.manufacturer
                    else " ".join(filter(None, [pending.manufacturer, row.manufacturer])),
                    "verified": pending.verified and row.verified,
                    "row_id": pending.row_id,
                }
            )
            pending, source_pages = None, []
        elif pending:
            warnings.append(
                Warning(
                    pdf_page=min(source_pages),
                    code="unclosed_carryover",
                    message="Продолжение предыдущей строки не подтверждено",
                    row=pending,
                )
            )
            pending, source_pages = None, []
        if row.continues_next:
            pending, source_pages = row, sources
            pending_header = article_header
            continue
        if not row.verified or not all(x.strip() for x in [row.caption, row.units, row.quantity]):
            warnings.append(
                Warning(
                    pdf_page=origin,
                    code="unverified_row",
                    message="Проверьте наименование, единицу и количество",
                    row=row,
                )
            )
            continue
        items.append(
            Evidence(
                item=SpecificationItem(
                    Caption=" ".join(
                        part.strip()
                        for part in [row.caption, row.designation, row.manufacturer]
                        if part.strip()
                    ),
                    Units=row.units.strip(),
                    Quantity=row.quantity.strip(),
                    UserComment1=project,
                    Pages=str(origin),
                    Brand=row.brand.strip(),
                    Article=row.article.strip(),
                    Note=row.note.strip(),
                ),
                source_pages=sources,
                row_id=row.row_id,
            )
        )
    carry = Carryover(
        pdf_page=page,
        pending=pending,
        source_pages=source_pages,
        group_context=result.group_context,
        article_header=pending_header if pending else result.article_header,
    )
    return items, warnings, carry
