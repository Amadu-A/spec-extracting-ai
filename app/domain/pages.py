# app/domain/pages.py
"""Парсинг физических PDF-страниц без неограниченного разворачивания диапазонов."""

import re

from app.domain.errors import ServiceError


def parse_pages(expression: str, total: int, limit: int) -> list[int]:
    """Нормализует диапазоны; проверяет границы до выделения списка чисел."""
    if not expression.strip():
        raise ServiceError("pages_required", "Укажите физические страницы PDF")
    if len(expression) > 4000:
        raise ServiceError("invalid_pages", "Слишком длинный список страниц")
    selected: set[int] = set()
    for token in expression.split(","):
        match = re.fullmatch(r"\s*([0-9]{1,6})\s*(?:-\s*([0-9]{1,6}))?\s*", token)
        if not match:
            raise ServiceError("invalid_pages", "Формат страниц: 1-5, 8, 12-16")
        start = int(match[1])
        end = int(match[2] or match[1])
        if start < 1 or end < start or end > total:
            raise ServiceError("pages_out_of_bounds", f"Допустимы страницы 1–{total}")
        if end - start + 1 > limit:
            raise ServiceError("page_limit", f"За один запуск разрешено не более {limit} страниц")
        selected.update(range(start, end + 1))
        if len(selected) > limit:
            raise ServiceError("page_limit", f"За один запуск разрешено не более {limit} страниц")
    return sorted(selected)
