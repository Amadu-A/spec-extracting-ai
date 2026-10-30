# tests/test_browser.py
"""Настоящий Chromium E2E с локальным HTTP/worker, реальные PDF PNG и replayable SSE.

Модель здесь — явно детерминированный test double. Реальный shared-vlm проверяется
отдельным live прогоном, его результаты не смешиваются с browser-test assertions.
"""

import asyncio
import contextlib
import socket
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import uvicorn

from app.presentation.http.app import create_app


@pytest.fixture
def web_server(root):
    """Поднимает локальный сервер и bounded test worker с теми же production use-case."""
    application = create_app(root)
    original = application.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        """Привязывает dispatcher к loop сервера и корректно останавливает его."""
        async with original(app):

            async def dispatch():
                """Только тестовый транспорт заменяет AMQP, бизнес-обработчик остаётся настоящим."""
                while True:
                    for job_id in await root.repository.dispatchable(10):
                        await root.processor.execute(job_id)
                    await asyncio.sleep(0.05)

            worker = asyncio.create_task(dispatch())
            yield
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker

    application.router.lifespan_context = lifespan
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.mark.browser
def test_ui_pdf_stream_exports_prompt_and_controls(root, pdf_bytes, web_server):
    """Проверяет рабочие controls, server prompt, навигацию, журнал и оба скачивания."""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=playwright.chromium.executable_path)
        page = browser.new_page(viewport={"width": 1672, "height": 941}, device_scale_factor=1)
        page.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined})")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_server)
        expect(page.locator("[data-login-dialog]")).to_have_count(0)
        page.locator("[data-prompt-open]").click()
        expect(page.locator("[data-prompt-dialog]")).to_be_visible()
        prompt = page.locator("[data-prompt-text]")
        original = prompt.input_value()
        prompt.fill("Тестовые инструкции текущей сессии")
        page.locator("[data-prompt-save]").click()
        page.locator("[data-prompt-open]").click()
        expect(prompt).to_have_value("Тестовые инструкции текущей сессии")
        page.locator("[data-prompt-restore]").click()
        expect(prompt).to_have_value(original)
        Path("test-results").mkdir(exist_ok=True)
        page.screenshot(path="test-results/prompt-modal.png")
        page.locator("[data-prompt-close]").first.click()
        page.locator("[data-file]").set_input_files(
            {"name": "project.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes}
        )
        expect(page.locator("[data-page-image]")).to_be_visible()
        page.wait_for_function("document.querySelector('[data-page-image]').naturalWidth > 0")
        page.locator("[data-pages]").fill("0")
        page.locator("[data-project]").fill("PROJECT")
        page.locator("[data-start]").click()
        expect(page.locator("[data-form-error]")).to_contain_text("Допустимы")
        page.locator("[data-pages]").fill("1-2")
        page.locator("[data-start]").click()
        expect(page.locator("[data-job-status]")).to_contain_text("Анализ завершён", timeout=20000)
        expect(page.locator("[data-item-count]")).to_have_text("2")
        expect(page.locator("[data-progress]")).to_have_text("100%")
        expect(page.locator('[data-export="json"]')).to_be_enabled()
        assert root.model.calls[0]["prompt"] == original
        for extension in ("json", "xlsx"):
            with page.expect_download() as download:
                page.locator(f'[data-export="{extension}"]').click()
            assert download.value.suggested_filename.endswith("." + extension)
        page.locator("[data-page-number]").fill("3")
        page.locator("[data-page-number]").press("Tab")
        expect(page.locator("[data-page-number]")).to_have_value("3")
        page.locator("[data-page-prev]").click()
        expect(page.locator("[data-page-number]")).to_have_value("2")
        page.locator("[data-page-next]").click()
        expect(page.locator("[data-page-number]")).to_have_value("3")
        page.locator("[data-zoom-in]").click()
        expect(page.locator("[data-zoom]")).to_have_text("125%")
        page.locator("[data-zoom-out]").click()
        expect(page.locator("[data-zoom]")).to_have_text("100%")
        page.locator("[data-fit]").click()
        page.locator("[data-hand]").click()
        expect(page.locator("[data-hand]")).to_have_attribute("aria-pressed", "true")
        page.locator("[data-zoom-in]").click()
        canvas = page.locator("[data-canvas]")
        canvas.evaluate("element => { element.scrollLeft = 30; }")
        rectangle = canvas.bounding_box()
        before_pan = canvas.evaluate("element => element.scrollLeft")
        page.mouse.move(rectangle["x"] + 200, rectangle["y"] + 150)
        page.mouse.down()
        page.mouse.move(rectangle["x"] + 140, rectangle["y"] + 150)
        page.mouse.up()
        assert canvas.evaluate("element => element.scrollLeft") > before_pan
        page.locator("[data-fullscreen]").click()
        page.wait_for_function("Boolean(document.fullscreenElement)")
        page.locator("[data-fullscreen]").click()
        page.wait_for_function("!document.fullscreenElement")
        page.locator("[data-journal-toggle]").click()
        expect(page.locator("[data-journal]")).not_to_be_visible()
        page.locator("[data-journal-toggle]").click()
        expect(page.locator("[data-journal]")).to_be_visible()
        page.locator("[data-menu-toggle]").click()
        page.locator("[data-toggle-view]").click()
        expect(page.locator("[data-stream-state]")).to_contain_text("Подтверждённый")
        page.locator("[data-warnings-open]").click()
        expect(page.locator("[data-warnings-dialog]")).to_be_visible()
        page.locator("[data-warnings-close]").click()
        page.screenshot(path="test-results/analysis-page.png")
        page.locator("[data-thumbnails] button").first.click()
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator("[data-retry]").click()
        expect(page.locator("[data-job-status]")).to_contain_text("Анализ завершён", timeout=20000)
        expect(page.locator("[data-item-count]")).to_have_text("2")
        page.locator("[data-file-remove]").click()
        expect(page.locator("[data-item-count]")).to_have_text("0")
        expect(page.locator("[data-journal]")).to_contain_text("Фактические этапы появятся")
        for width in (800, 375):
            page.set_viewport_size({"width": width, "height": 950})
            page.screenshot(path=f"test-results/responsive-{width}.png")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (
                page.evaluate(
                    "({width:innerWidth, scroll:document.documentElement.scrollWidth, overflow:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,8).map(e=>[e.tagName,e.className,e.getBoundingClientRect().width])})"
                )
            )
            expect(page.locator("[data-prompt-open]")).to_be_visible()
        page.goto(web_server + "/docs")
        expect(
            page.get_by_role("heading").filter(has_text="Извлечение спецификаций")
        ).to_be_visible()
        expect(page.get_by_role("button", name="Authorize", exact=True)).to_be_visible()
        assert not errors
        browser.close()


@pytest.mark.browser
def test_expired_cookie_recovers_without_login_window(root, pdf_bytes, web_server):
    """Потеря cookie очищает прежний PDF, автоматически создаёт сессию и позволяет новую загрузку."""
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=playwright.chromium.executable_path)
        context = browser.new_context()
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_server)
        expect(
            page.locator("input[type=password], [data-login-dialog], [data-logout]")
        ).to_have_count(0)
        with page.expect_response("**/api/v1/documents") as uploaded:
            page.locator("[data-file]").set_input_files(
                {"name": "private.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes}
            )
        assert uploaded.value.status == 201
        identifier = uploaded.value.json()["id"]
        expect(page.locator("[data-page-image]")).to_be_visible()
        context.clear_cookies()
        page.locator("[data-prompt-open]").click()
        expect(page.locator("[data-file-meta]")).to_contain_text("Сессия обновлена")
        expect(page.locator("[data-page-image]")).not_to_be_visible()
        expect(page.locator("dialog[open]")).to_have_count(0)
        assert any(cookie["name"] == "spec_session" for cookie in context.cookies())
        assert page.request.get(f"{web_server}/api/v1/documents/{identifier}").status == 404
        page.locator("[data-file]").set_input_files(
            {"name": "new.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes}
        )
        expect(page.locator("[data-page-image]")).to_be_visible()
        expect(page.locator("[data-file-name]")).to_have_text("new.pdf")
        assert not errors
        browser.close()


@pytest.mark.browser
def test_browser_manual_view_until_next_vlm_page_and_refresh(root, pdf_bytes, web_server):
    """Настоящий SSE не отбирает просмотр на токенах/reconnect, новый page_started переключает."""
    from playwright.sync_api import expect, sync_playwright

    root.model.delay = 0.5
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=playwright.chromium.executable_path)
        page = browser.new_page(viewport={"width": 1672, "height": 941})
        page.goto(web_server)
        expect(page.locator("[data-login-dialog]")).to_have_count(0)
        page.locator("[data-file]").set_input_files(
            {"name": "view.pdf", "mimeType": "application/pdf", "buffer": pdf_bytes}
        )
        expect(page.locator("[data-page-image]")).to_be_visible()
        page.locator("[data-pages]").fill("1-2")
        page.locator("[data-project]").fill("P")
        page.locator("[data-start]").click()
        expect(page.locator("[data-current-page]")).to_have_text("1", timeout=10000)
        page.locator("[data-page-number]").fill("3")
        expect(page.locator("[data-code]")).to_contain_text("row_id", timeout=3000)
        expect(page.locator("[data-page-number]")).to_have_value("3")
        page.locator("[data-page-number]").press("Tab")
        expect(page.locator("[data-page-number]")).to_have_value("3")
        page.reload()
        expect(page.locator("[data-page-number]")).to_have_value("3")
        expect(page.locator("[data-current-page]")).to_have_text("2", timeout=15000)
        expect(page.locator("[data-page-number]")).to_have_value("2")
        page.locator("[data-thumbnails] button").nth(2).click()
        expect(page.locator("[data-job-status]")).to_contain_text("Анализ завершён", timeout=15000)
        expect(page.locator("[data-page-number]")).to_have_value("3")
        browser.close()


@pytest.mark.browser
@pytest.mark.live
def test_real_pdf_vlm_ui_json_excel(root, web_server, tmp_path, request):
    """Реальный PDF/VLM/UI/export путь; только AMQP transport заменён локальным dispatcher."""
    if not request.config.getoption("--vlm"):
        pytest.skip("Не указан --vlm")
    import json

    from openpyxl import load_workbook
    from playwright.sync_api import expect, sync_playwright

    from app.infrastructure.vlm import SharedVlmClient

    source = next(
        (path for path in Path("examples").glob("*.pdf") if path.stem.endswith(".ТМ")), None
    )
    if source is None:
        pytest.skip("Нужен исходный комплект ТМ в examples")
    model = SharedVlmClient(root.settings)
    root.model = model
    root.processor.model = model
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=playwright.chromium.executable_path)
        page = browser.new_page(viewport={"width": 1672, "height": 941})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(web_server)
        expect(page.locator("[data-login-dialog]")).to_have_count(0)
        page.locator("[data-file]").set_input_files(str(source.resolve()))
        expect(page.locator("[data-page-image]")).to_be_visible()
        page.locator("[data-pages]").fill("1")
        page.locator("[data-project]").fill("267-РД-ИТП.ТМ")
        page.locator("[data-start]").click()
        expect(page.locator('[data-export="json"]')).to_be_enabled(timeout=240000)
        assert int(page.locator("[data-item-count]").inner_text()) > 0
        outputs = {}
        for extension in ("json", "xlsx"):
            with page.expect_download() as download:
                page.locator(f'[data-export="{extension}"]').click()
            path = tmp_path / f"real-result.{extension}"
            download.value.save_as(path)
            outputs[extension] = path
        result = json.loads(outputs["json"].read_text(encoding="utf8"))
        sheet = load_workbook(outputs["xlsx"]).worksheets[0]
        assert list(sheet.values)[1:] == [tuple(item.values()) for item in result["items"]]
        assert all(item["Pages"] == "1" for item in result["items"])
        page.locator("[data-menu-toggle]").click()
        page.locator("[data-toggle-view]").click()
        Path("test-results").mkdir(exist_ok=True)
        page.screenshot(path="test-results/real-vlm-analysis.png")
        page.locator("[data-prompt-open]").click()
        expect(page.locator("[data-prompt-dialog]")).to_be_visible()
        page.screenshot(path="test-results/real-vlm-prompt.png")
        assert not errors
        browser.close()
