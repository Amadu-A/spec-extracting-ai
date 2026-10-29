# app/domain/quality.py
"""Сравнение с непроверенным baseline: мультисеты, поля и физические страницы.

Точное совпадение с baseline — регрессионная метрика, а не доказанная OCR accuracy.
Различия не исправляют эталон автоматически и не меняют результаты извлечения.
"""

from collections import Counter
from difflib import SequenceMatcher

from app.domain.models import Export

FIELDS = ("Caption", "Units", "Quantity", "UserComment1", "Pages")


def compare_exports(expected: Export, actual: Export) -> dict:
    """Сопоставляет строки с учётом кратности и возвращает все добавления/пропуски.

    Похожие строки связываются только внутри одной физической страницы для
    удобства проверки человеком. Они не засчитываются как точные совпадения.
    """
    expected_rows = [tuple(getattr(row, field) for field in FIELDS) for row in expected.items]
    actual_rows = [tuple(getattr(row, field) for field in FIELDS) for row in actual.items]
    expected_counts, actual_counts = Counter(expected_rows), Counter(actual_rows)
    matched = sum((expected_counts & actual_counts).values())
    missing = list((expected_counts - actual_counts).elements())
    added = list((actual_counts - expected_counts).elements())
    changed = []
    remaining_missing = []
    for before in missing:
        candidates = [
            (
                SequenceMatcher(None, before[0].casefold(), after[0].casefold()).ratio(),
                sum(before[column] == after[column] for column in (1, 2, 3)),
                i,
            )
            for i, after in enumerate(added)
            if before[4] == after[4]
        ]
        best = max(candidates, default=(0, 0, -1))
        if best[0] >= 0.55:
            after = added.pop(best[2])
            changed.append(
                {
                    "pdf_page": before[4],
                    "similarity": round(best[0], 4),
                    "expected": dict(zip(FIELDS, before, strict=True)),
                    "actual": dict(zip(FIELDS, after, strict=True)),
                    "changed_fields": [
                        field
                        for field, left, right in zip(FIELDS, before, after, strict=True)
                        if left != right
                    ],
                }
            )
        else:
            remaining_missing.append(before)
    pages = sorted({row[4] for row in expected_rows + actual_rows}, key=int)
    per_page = []
    for number in pages:
        left, right = (
            Counter(row for row in expected_rows if row[4] == number),
            Counter(row for row in actual_rows if row[4] == number),
        )
        per_page.append(
            {
                "pdf_page": int(number),
                "baseline": sum(left.values()),
                "actual": sum(right.values()),
                "exact": sum((left & right).values()),
            }
        )
    precision = matched / len(actual_rows) if actual_rows else (1 if not expected_rows else 0)
    recall = matched / len(expected_rows) if expected_rows else 1
    return {
        "baseline_items": len(expected_rows),
        "actual_items": len(actual_rows),
        "exact_matches": matched,
        "exact_precision": round(precision, 4),
        "exact_recall": round(recall, 4),
        "exact_f1": round(2 * precision * recall / (precision + recall), 4)
        if precision + recall
        else 0,
        "changed": changed,
        "missing": [dict(zip(FIELDS, row, strict=True)) for row in remaining_missing],
        "added": [dict(zip(FIELDS, row, strict=True)) for row in added],
        "pages": per_page,
        "baseline_is_verified_golden": False,
    }
