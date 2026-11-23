# tests/test_invoice_browser.py
"""Настоящий Chromium проверяет оба профиля; модель offline-теста является явным double."""

import json
import re
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import pymupdf
import pytest
from openpyxl import load_workbook

from app.bootstrap import configure_invoices
from app.infrastructure.vlm import SharedVlmClient
from tests.invoice_helpers import InvoiceModelDouble
from tests.test_browser import web_server as web_server  # noqa: F401
from tests.test_live_invoices import compare_files, verify_enrichments


def assert_financial_workbook(book, result):
    """Сверяет экспорт с JSON по независимому публичному порядку финансовых колонок."""
    position_columns = {
        13: "quantity",
        14: "unit_price_without_vat",
        15: "unit_price_with_vat",
        16: "line_total_without_vat",
        17: "vat_rate",
        18: "vat_amount",
        19: "line_total_with_vat",
    }

    def number(cell, expected):
        """Пустое значение не превращается в ноль; числа сохраняют исходную точность."""
        if expected is None:
            assert cell.value is None
        else:
            assert cell.data_type != "f"
            assert Decimal(str(cell.value)) == Decimal(expected)

    document_row = position_row = 2
    for file in result["files"]:
        for document in file["documents"]:
            for column, field in ((14, "without_vat"), (15, "vat"), (16, "with_vat")):
                number(book["Документы"].cell(document_row, column), document["totals"][field])
            document_row += 1
            for item in document["items"]:
                for column, field in position_columns.items():
                    number(book["Позиции"].cell(position_row, column), item[field])
                assert book["Позиции"].cell(position_row, 21).value == ", ".join(
                    map(str, item["source_pages"])
                )
                position_row += 1
    assert book["Документы"].max_row == document_row - 1
    assert book["Позиции"].max_row == position_row - 1


def assert_stream_panel_bounds(profile):
    """Поток остаётся внутри своей карточки и не перекрывает журнал при длинной сводке."""
    result = profile.locator(".result").bounding_box()
    code = profile.locator(".result .code").bounding_box()
    journal = profile.locator(".journal").bounding_box()
    assert code["y"] + code["height"] <= result["y"] + result["height"]
    assert result["y"] + result["height"] <= journal["y"]


@pytest.mark.browser
def test_invoice_ui_batch_prompt_exports_reload_and_profile_isolation(root, web_server, tmp_path):
    """Мультизагрузка, viewer, два prompt, JSON/XLSX, reload и живое переключение профиля."""
    from playwright.sync_api import expect, sync_playwright

    model = InvoiceModelDouble()
    model.delay = 0.015
    configure_invoices(root, model)
    with pymupdf.open() as document:
        for index in range(3):
            document.new_page().insert_text((60, 60), f"Synthetic invoice page {index + 1}")
        data = document.tobytes()
        image = document[0].get_pixmap().tobytes("png")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1672, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_server)
        page.locator('[data-profile="invoice"][data-profile-ready="true"]').click()
        profile = page.locator("[data-invoice-profile]")
        expect(profile).to_be_visible()
        expect(profile.locator("[data-project]")).to_have_count(0)
        profile.locator("[data-prompt-open]").click()
        expect(profile.locator("[data-prompt-dialog]")).to_be_visible()
        text = profile.locator("[data-prompt-text]")
        original = text.input_value()
        text.fill("Invoice-only instructions")
        profile.locator("[data-prompt-save]").click()
        page.locator('[data-profile="spec"]').click()
        page.locator("[data-spec-profile] [data-prompt-open]").click()
        expect(page.locator("[data-spec-profile] [data-prompt-text]")).not_to_have_value(
            "Invoice-only instructions"
        )
        page.locator("[data-spec-profile] [data-prompt-close]").first.click()
        page.locator('[data-profile="invoice"]').click()
        profile.locator("[data-invoice-files]").set_input_files(
            [
                {"name": "invoice-a.pdf", "mimeType": "application/pdf", "buffer": data},
                {"name": "scan-b.png", "mimeType": "image/png", "buffer": image},
            ]
        )
        expect(profile.locator("[data-invoice-file-list] article")).to_have_count(2)
        profile.locator("[data-invoice-start]").click()
        page.locator('[data-profile="spec"]').click()
        expect(page.locator("[data-spec-profile]")).to_be_visible()
        page.locator('[data-profile="invoice"]').click()
        expect(profile.locator("[data-invoice-status]")).to_have_attribute(
            "data-status", "completed", timeout=30000
        )
        expect(profile.locator("[data-invoice-documents] tr")).to_have_count(2)
        assert_stream_panel_bounds(profile)
        assert len(model.calls) == 4
        assert model.calls[0]["prompt"] == "Invoice-only instructions"
        profile.locator("[data-invoice-file-list] article button").first.click()
        expect(profile.locator("[data-page-image]")).to_be_visible()
        profile.locator("[data-page-number]").fill("2")
        profile.locator("[data-page-number]").dispatch_event("change")
        expect(profile.locator("[data-page-number]")).to_have_value("2")
        profile.locator("[data-zoom-in]").click()
        expect(profile.locator("[data-zoom]")).to_have_text("125%")
        saved = {}
        for extension in ("json", "xlsx"):
            with page.expect_download() as event:
                profile.locator(f'[data-invoice-export="{extension}"]').click()
            path = tmp_path / f"invoice.{extension}"
            event.value.save_as(path)
            saved[extension] = path.read_bytes()
        exported = json.loads(saved["json"])
        assert exported["summary"]["item_count"] == 2
        assert load_workbook(BytesIO(saved["xlsx"])).sheetnames == [
            "Документы",
            "Позиции",
            "Предупреждения",
        ]
        assert_financial_workbook(load_workbook(BytesIO(saved["xlsx"])), exported)
        page.reload()
        expect(page.locator("[data-invoice-profile]")).to_be_visible()
        expect(page.locator("[data-invoice-profile] [data-invoice-documents] tr")).to_have_count(2)
        expect(page.locator("[data-invoice-profile] [data-page-number]")).to_have_value("2")
        assert len(model.calls) == 4
        profile.locator("[data-prompt-open]").click()
        expect(text).to_have_value("Invoice-only instructions")
        profile.locator("[data-prompt-restore]").click()
        expect(text).to_have_value(original)
        profile.locator("[data-prompt-close]").first.click()
        output = Path("reports/live/invoices/browser")
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output / "invoice-ui.png"), full_page=True)
        assert not errors, errors
        browser.close()


@pytest.mark.browser
@pytest.mark.live
@pytest.mark.corpus
def test_real_invoice_batch_via_ui_and_api(root, web_server, tmp_path, request):
    """Все семь реальных PDF проходят загрузку UI, настоящий VLM, JSON/API и финансовый Excel."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Нужен --vlm для настоящей модели")
    paths = sorted(Path("examples/invoices").glob("*.pdf"))
    if not paths and not request.config.getoption("--require-invoice-corpus"):
        pytest.skip("Локальный invoice-корпус отсутствует")
    assert len(paths) == 7, "Нужны семь PDF/JSON в examples/invoices"
    from playwright.sync_api import expect, sync_playwright

    root.model = SharedVlmClient(root.settings)
    root.settings.vlm_attempts = 3
    configure_invoices(root)
    output = Path("reports/live/invoices/browser-live-batch")
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1672, "height": 1100})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_server)
        page.locator('[data-profile="invoice"][data-profile-ready="true"]').click()
        profile = page.locator("[data-invoice-profile]")
        profile.locator("[data-invoice-files]").set_input_files(
            [str(path.resolve()) for path in paths]
        )
        profile.locator("[data-invoice-start]").click()
        expect(profile.locator("[data-invoice-status]")).to_have_attribute(
            "data-status",
            re.compile(r"^(completed|completed_with_warnings|failed|cancelled)$"),
            timeout=1800000,
        )
        saved = page.evaluate("() => JSON.parse(sessionStorage.getItem('invoice.job'))")
        base = f"{web_server}/api/v1/invoice-extractions/{saved['jobId']}"
        status = page.request.get(base).json()
        (output / "status.json").write_text(
            json.dumps(status, ensure_ascii=False, indent=2), encoding="utf8"
        )
        snapshot = page.request.get(base + "/snapshot").json()
        (output / "snapshot.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf8"
        )
        assert status["status"] in ("completed", "completed_with_warnings"), status["error_code"]
        response = page.request.get(base + "/result.json")
        assert response.status == 200
        result = response.json()
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
        )
        reports = []
        for path, file in zip(paths, result["files"], strict=True):
            baseline = json.loads(path.with_suffix(".json").read_text(encoding="utf8"))
            formatting, enrichments = [], []
            differences = compare_files(baseline["files"][0], file, formatting, enrichments)
            reports.append(
                dict(
                    file=path.name,
                    expected_items=baseline["summary"]["item_count"],
                    actual_items=sum(len(doc["items"]) for doc in file["documents"]),
                    differences=differences,
                    formatting_differences=formatting,
                    source_enrichments=enrichments,
                    unverified_enrichments=verify_enrichments(path, enrichments),
                )
            )
        (output / "comparison.json").write_text(
            json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf8"
        )
        assert result["summary"] == dict(file_count=7, document_count=7, item_count=161)
        assert not any(
            report["differences"] or report["unverified_enrichments"] for report in reports
        ), "Различия с эталонами: reports/live/invoices/browser-live-batch/comparison.json"
        expect(profile.locator("[data-invoice-documents] tr")).to_have_count(7)
        assert_stream_panel_bounds(profile)
        with page.expect_download() as download:
            profile.locator('[data-invoice-export="xlsx"]').click()
        target = tmp_path / "invoices.xlsx"
        download.value.save_as(target)
        book = load_workbook(target)
        assert_financial_workbook(book, result)
        assert book["Документы"].max_row == 8 and book["Позиции"].max_row == 162
        row = 2
        for file in result["files"]:
            for doc in file["documents"]:
                for item in doc["items"]:
                    assert book["Позиции"].cell(row, 1).value == file["source_file"]
                    assert book["Позиции"].cell(row, 4).value == item["caption"]
                    row += 1
        page.screenshot(path=str(output / "invoice-ui-real.png"), full_page=True)
        page.reload()
        expect(page.locator("[data-invoice-profile] [data-invoice-documents] tr")).to_have_count(
            7, timeout=20000
        )
        assert not errors, errors
        browser.close()
