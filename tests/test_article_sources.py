# tests/test_article_sources.py
"""Эталоны Article из трёх JSON: 770 строк, 128 кодов, неизменность остальных полей.

Компактная разметка хранит только коды и hash семи прежних полей. Она работает
и со старыми JSON в checkout: реальные PDF/outputs не копируются в новые Git-файлы.
Реальная модель проверяется отдельно в test_live_corpus.py.
"""

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pytest

from app.domain.models import Export
from app.domain.reconcile import reconcile
from scripts.evaluate import find_pairs
from tests.test_domain import page, row

SEVEN_FIELDS = ("Caption", "Units", "Quantity", "UserComment1", "Pages", "Brand", "Note")
CODE_HEADER = "Код оборудования, изделия, материала"
FIXTURE = Path(__file__).parent / "fixtures/article-codes.json"


def expected_articles(name: str, baseline: Export) -> Export:
    """Применяет явную разметку кодов к baseline без изменения семи полей или файла."""
    annotation = json.loads(FIXTURE.read_text(encoding="utf8")).get(name)
    if annotation is None:
        return baseline
    assert len(baseline.items) == annotation["items"]
    seven = json.dumps(
        [[getattr(item, field) for field in SEVEN_FIELDS] for item in baseline.items],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf8")
    assert hashlib.sha256(seven).hexdigest() == annotation["seven_fields_sha256"], name
    items = []
    for index, item in enumerate(baseline.items, 1):
        article = annotation["articles"].get(str(index), "")
        assert not item.Article or item.Article == article, (name, index)
        items.append(item.model_copy(update={"Article": article}))
    return Export(file_name=baseline.file_name, items=items)


def compare_article_rows(expected: Export, actual: Export) -> dict:
    """Проверяет Article по физической странице и порядку строки, без подстановок в ответ модели."""
    left, right = defaultdict(list), defaultdict(list)
    for item in expected.items:
        left[item.Pages].append(item)
    for item in actual.items:
        right[item.Pages].append(item)
    count_mismatches, values = [], []
    matched, same_seven = 0, 0
    for number in sorted(left.keys() | right.keys(), key=int):
        before, after = left[number], right[number]
        if len(before) != len(after):
            count_mismatches.append(
                {"pdf_page": number, "expected": len(before), "actual": len(after)}
            )
        for index, (first, second) in enumerate(zip(before, after, strict=False), 1):
            same_seven += all(
                getattr(first, field) == getattr(second, field) for field in SEVEN_FIELDS
            )
            if first.Article == second.Article:
                matched += bool(first.Article)
            else:
                values.append(
                    {
                        "pdf_page": number,
                        "row": index,
                        "expected": first.model_dump(),
                        "actual": second.model_dump(),
                    }
                )
    return {
        "expected_items": len(expected.items),
        "actual_items": len(actual.items),
        "expected_filled": sum(bool(item.Article) for item in expected.items),
        "actual_filled": sum(bool(item.Article) for item in actual.items),
        "exact_nonempty": matched,
        "seven_fields_exact": same_seven,
        "page_count_mismatches": count_mismatches,
        "value_mismatches": values,
    }


@pytest.mark.parametrize(
    "header", ["Артикул", CODE_HEADER, "Код обору-\nдования, изделия, материала"]
)
def test_article_does_not_change_seven_other_fields(header):
    """Код не добавляется в Caption и не изменяет единицы, количество, бренд и примечание."""
    source = row(
        caption="Клапан", designation="DN100", manufacturer="Ридан", brand="Ридан", note="К1"
    )
    old = reconcile(3, "P", page([source]), None)[0][0].item
    new = reconcile(
        3,
        "P",
        page([source.model_copy(update={"article": "00042/Ab-1"})], article_header=header),
        None,
    )[0][0].item
    assert new.Article == "00042/Ab-1"
    assert all(getattr(old, field) == getattr(new, field) for field in SEVEN_FIELDS)


@pytest.mark.parametrize("header", ["Артикул", CODE_HEADER])
def test_code_carryover_and_conflict_keep_existing_rules(header):
    """Оба источника проходят соседние листы; противоречивые коды не выбираются произвольно."""
    first = reconcile(
        1,
        "P",
        page(
            [row(caption="Клапан", article="00042/Ab-1", continues_next=True)],
            article_header=header,
        ),
        None,
    )[2]
    items, warnings, _ = reconcile(
        2, "P", page([row(caption="DN100", article="00042/Ab-1", continuation=True)]), first
    )
    assert not warnings and items[0].item.Article == "00042/Ab-1" and items[0].item.Pages == "1"
    assert items[0].source_pages == [1, 2]
    assert not reconcile(2, "P", page([row(continuation=True, article="OTHER")]), first)[0]
    assert (
        reconcile(2, "P", page([row(continuation=True, article="OTHER")]), first)[1][0].code
        == "carryover_conflict"
    )


@pytest.mark.corpus
def test_all_770_baseline_rows_128_articles_and_seven_unchanged_fields():
    """Проверяет все утверждённые позиции чистым алгоритмом; это не оценка настоящей VLM."""
    annotations = json.loads(FIXTURE.read_text(encoding="utf8"))
    pairs = {
        pdf.stem.removeprefix("spec_"): baseline for pdf, baseline in find_pairs(Path("examples"))
    }
    if not annotations.keys() <= pairs.keys():
        pytest.skip("Нужны три локальных комплекта examples")
    count, filled = 0, 0
    for name, annotation in annotations.items():
        baseline = Export.model_validate_json(pairs[name].read_text(encoding="utf-8-sig"))
        expected = expected_articles(name, baseline)
        by_page = defaultdict(list)
        for index, item in enumerate(expected.items, 1):
            by_page[item.Pages].append(
                row(
                    row_id=str(index),
                    caption=item.Caption,
                    designation="",
                    manufacturer="",
                    units=item.Units,
                    quantity=item.Quantity,
                    brand=item.Brand,
                    article=item.Article,
                    note=item.Note,
                )
            )
        items = []
        for number, rows in sorted(by_page.items(), key=lambda pair: int(pair[0])):
            found, warnings, _ = reconcile(
                int(number),
                expected.items[0].UserComment1,
                page(rows, article_header=CODE_HEADER),
                None,
            )
            assert not warnings
            items.extend(evidence.item for evidence in found)
        actual = Export(items=items)
        assert actual == expected
        assert len(items) == annotation["items"]
        count += len(items)
        filled += sum(bool(item.Article) for item in items)
    assert count == 770 and filled == 128
