# app/infrastructure/pdf_task.py
"""Изолированный subprocess PyMuPDF: ограничивает CPU/RAM и не исполняет PDF JS.

Получает JSON через stdin; возвращает JSON/PNG в base64. Доступ к файлу уже
проверен storage-адаптером, пользовательские пути в этот процесс не поступают.
"""

import base64
import json
import math
import re
import sys
from datetime import UTC, datetime
from time import perf_counter

import pymupdf


def constrain_process() -> None:
    """На Linux ограничивает память и CPU нативного парсера отдельного процесса."""
    if sys.platform == "linux":
        import resource

        # Два процесса разбора должны помещаться вместе с API в лимит контейнера 2 ГБ.
        resource.setrlimit(resource.RLIMIT_AS, (512_000_000, 512_000_000))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 35))


def raster(page, scale: float, pixel_limit: int, clip=None) -> bytes:
    """Уменьшает масштаб до создания bitmap, защищая A0/A1 от OOM."""
    rect = clip or page.rect
    safe_scale = min(scale, math.sqrt(pixel_limit / max(1, rect.width * rect.height)))
    return page.get_pixmap(
        matrix=pymupdf.Matrix(safe_scale, safe_scale), clip=clip, alpha=False
    ).tobytes("png")


def execute(payload: dict) -> dict:
    """Проверяет PDF и выполняет единственную ограниченную операцию."""
    try:
        document = pymupdf.open(payload["path"])
    except Exception:
        return {"error": "corrupted_pdf", "message": "Повреждённый или неподдерживаемый PDF"}
    with document:
        if not document.is_pdf:
            return {"error": "unsupported_file", "message": "Документ не является PDF"}
        if document.needs_pass:
            return {"error": "password_pdf", "message": "PDF с паролем не поддерживается"}
        if not len(document):
            return {"error": "empty_pdf", "message": "PDF не содержит страниц"}
        if len(document) > payload["max_pages"]:
            return {"error": "pdf_page_limit", "message": "Превышен лимит страниц PDF"}
        if document.is_repaired:
            return {
                "error": "corrupted_pdf",
                "message": "Структура PDF повреждена: пересохраните оригинал",
            }
        if payload["operation"] == "inspect":
            texts = [document[i].get_text()[:50000] for i in range(min(3, len(document)))]
            codes = re.findall(
                r"\b\d{2,6}[-–][А-Яа-яA-Za-z]{1,5}[-–][А-Яа-яA-Za-z0-9.]{2,30}", "\n".join(texts)
            )
            return {
                "page_count": len(document),
                "suggested_project_code": max(set(codes), key=codes.count) if codes else None,
            }
        number = payload["pdf_page"]
        if not 1 <= number <= len(document):
            return {"error": "pages_out_of_bounds", "message": "Страница вне PDF"}
        page = document[number - 1]
        if max(page.rect.width, page.rect.height) > payload["max_page_points"]:
            return {
                "error": "page_geometry_limit",
                "message": "Физический размер страницы превышает безопасный лимит",
            }
        if payload["operation"] == "preview":
            image = raster(
                page, payload["width"] / page.rect.width, min(payload["max_pixels"], 4_000_000)
            )
            return {"image": base64.b64encode(image).decode()}
        text_started_at, text_started = datetime.now(UTC), perf_counter()
        text = page.get_text(sort=True)[:60000]
        blocks = [
            {"bbox": list(b[:4]), "text": b[4][:2000]}
            for b in page.get_text("blocks")[:2000]
            if isinstance(b[4], str)
        ]
        text_duration = (perf_counter() - text_started) * 1000
        render_started_at, render_started = datetime.now(UTC), perf_counter()
        scale = payload["dpi"] / 72
        images = [raster(page, scale, payload["max_pixels"])]
        warnings = []
        if len(text.strip()) < 150:
            warnings.append("image_only: текстовый слой недостаточен, используется vision")
        if page.rect.width * page.rect.height * scale * scale > payload["max_pixels"]:
            warnings.append("adaptive_crops: общий лист уменьшен, добавлены детальные половины")
            vertical = page.rect.height >= page.rect.width
            for side in range(2):
                rect = pymupdf.Rect(page.rect)
                if vertical:
                    rect.y0, rect.y1 = (
                        side * page.rect.height / 2,
                        (side + 1) * page.rect.height / 2,
                    )
                else:
                    rect.x0, rect.x1 = side * page.rect.width / 2, (side + 1) * page.rect.width / 2
                images.append(raster(page, scale, payload["max_pixels"], rect))
        return {
            "text": text,
            "blocks": blocks,
            "warnings": warnings,
            "images": [base64.b64encode(image).decode() for image in images],
            "stages": [
                {
                    "name": "Извлечение текста",
                    "state": "completed",
                    "started_at": text_started_at.isoformat(),
                    "duration_ms": round(text_duration, 2),
                },
                {
                    "name": "Рендер страницы",
                    "state": "completed",
                    "started_at": render_started_at.isoformat(),
                    "duration_ms": round((perf_counter() - render_started) * 1000, 2),
                },
            ],
        }


def main() -> None:
    """Исполняет JSON-протокол без утечки traceback или текста документа в stderr."""
    constrain_process()
    try:
        result = execute(json.loads(sys.stdin.buffer.read(10000)))
    except Exception:
        result = {
            "error": "pdf_processing_failed",
            "message": "Не удалось безопасно обработать PDF",
        }
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
