# tests/test_quality.py
"""Мультисетная регрессия, проверка всех четырёх локальных исходных комплектов."""

import json
from collections import Counter
from pathlib import Path

import pymupdf
import pytest

from app.domain.models import Export, SpecificationItem
from app.domain.quality import compare_exports, compare_reviewed_fields
from scripts.evaluate import find_pairs


def test_quality_preserves_duplicates_and_field_changes():
    """Количество одинаковых позиций учитывается; похожие имена не становятся exact match."""
    item = SpecificationItem(
        Caption="Клапан DN100 Ридан", Units="шт", Quantity="2", UserComment1="P", Pages="1"
    )
    expected = Export(items=[item, item])
    actual = Export(items=[item, item.model_copy(update={"Quantity": "3"})])
    result = compare_exports(expected, actual)
    assert result["exact_matches"] == 1 and result["exact_f1"] == 0.5
    assert result["changed"][0]["changed_fields"] == ["Quantity"]
    assert result["pages"] == [{"pdf_page": 1, "baseline": 2, "actual": 2, "exact": 1}]
    assert not result["baseline_is_verified_golden"]


@pytest.mark.corpus
@pytest.mark.parametrize(
    ("suffix", "pages", "count"),
    [("ТМ", 31, 488), ("ЭОМ", 11, 197), ("АТМД", 5, 85), ("УУТЭ", 4, 32)],
)
def test_actual_document_baseline_contract(suffix, pages, count):
    """Все предоставленные PDF читаются; baseline проверяется как контракт, не как OCR-истина."""
    pairs = find_pairs(Path("examples"))
    pair = next((pair for pair in pairs if pair[0].stem.endswith("." + suffix)), None)
    if not pair:
        pytest.skip("Локальный комплект examples не предоставлен")
    pdf, baseline = pair
    with pymupdf.open(pdf) as source:
        assert len(source) == pages
    result = Export.model_validate_json(baseline.read_text(encoding="utf-8-sig"))
    assert len(result.items) == count
    assert all(1 <= int(row.Pages) <= pages for row in result.items)
    if suffix == "УУТЭ":
        assert dict(Counter(row.Pages for row in result.items)) == {"1": 22, "4": 10}
    assert compare_exports(result, result)["exact_f1"] == 1
    legacy = Export.model_validate(
        {
            "items": [
                {
                    field: value
                    for field, value in item.model_dump().items()
                    if field in ("Caption", "Units", "Quantity", "UserComment1", "Pages")
                }
                for item in result.items
            ]
        }
    )
    assert compare_exports(legacy, result)["exact_f1"] == 1
    samples = json.loads(Path("tests/fixtures/additional-fields.json").read_text(encoding="utf-8"))
    # Старый baseline подтверждает только существование позиции, не отсутствие новых граф.
    reviewed = compare_reviewed_fields(samples[pdf.stem.removeprefix("spec_")], legacy)
    assert reviewed["located_rows"] == reviewed["reviewed_rows"]
    assert all(item.Brand == item.Article == item.Note == "" for item in legacy.items)


def test_additional_comparison_does_not_hide_missing_or_ambiguous_rows():
    """Ручные поля сравниваются отдельно; неоднозначность и пропуск не дают ложное совпадение."""
    item = SpecificationItem(
        Caption="Клапан", Units="шт", Quantity="2", UserComment1="P", Pages="1", Brand="Завод"
    )
    sample = {
        "Pages": "1",
        "Caption_contains": ["Клапан"],
        "expected": {"Brand": "Завод", "Article": "", "Note": ""},
    }
    for rows in ([], [item, item]):
        result = compare_reviewed_fields([sample], Export(items=rows))
        assert result["located_rows"] == 0 and not any(result["exact_fields"].values())
    result = compare_reviewed_fields(
        [sample], Export(items=[item.model_copy(update={"Brand": "Другой"})])
    )
    assert result["located_rows"] == 1 and result["exact_fields"] == {
        "Brand": 0,
        "Article": 1,
        "Note": 1,
    }
