# tests/test_quality.py
"""Мультисетная регрессия, проверка всех четырёх локальных исходных комплектов."""

from collections import Counter
from pathlib import Path

import pymupdf
import pytest

from app.domain.models import Export, SpecificationItem
from app.domain.quality import compare_exports
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
