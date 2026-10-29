# tests/test_adapters.py
"""Контракты VLM streaming, PDF image-only/большого листа, Excel injection и конфигурации."""

import json
from io import BytesIO

import httpx
import pymupdf
import pytest
from openpyxl import load_workbook

from app.application.ports import PreparedPage
from app.core.settings import Settings
from app.domain.errors import ServiceError
from app.domain.models import Export, SpecificationItem
from app.infrastructure.excel import build_excel
from app.infrastructure.vlm import SharedVlmClient


@pytest.mark.parametrize(
    ("finish", "expected"),
    [("stop", None), ("length", "vlm_truncated"), (None, "vlm_stream_incomplete")],
)
async def test_vlm_wire_contract(finish, expected):
    """Настоящий HTTP adapter отправляет image messages/schema и проверяет finish_reason."""
    requests = []

    def handle(request):
        """Заменяет только HTTP server; проверяет production payload и SSE decoder."""
        requests.append(request)
        stream = (
            "data: "
            + json.dumps(
                {"choices": [{"delta": {"content": '{"rows":[]}'}, "finish_reason": finish}]}
            )
            + "\n\ndata: [DONE]\n\n"
        )
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    model = SharedVlmClient(Settings(_env_file=None), http)

    async def read():
        """Собирает поток клиента независимо от extraction use-case."""
        return "".join(
            [
                part
                async for part in model.stream(
                    PreparedPage("text", [], [b"png"], []), "task", {"pdf_page": 1}
                )
            ]
        )

    if expected:
        with pytest.raises(ServiceError) as error:
            await read()
        assert error.value.code == expected
    else:
        assert await read() == '{"rows":[]}'
    payload = json.loads(requests[0].content)
    assert payload["model"] == "shared-vlm" and payload["response_format"]["type"] == "json_schema"
    assert payload["messages"][1]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert "authorization" not in requests[0].headers
    await model.close()


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "vlm_contract_unsupported"),
        (429, "vlm_unavailable"),
        (503, "vlm_unavailable"),
        (401, "vlm_rejected"),
    ],
)
async def test_vlm_http_errors(status, code):
    """HTTP отказы не превращаются в успешный пустой JSON или текстовый fallback."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    model = SharedVlmClient(Settings(_env_file=None), http)
    with pytest.raises(ServiceError) as error:
        async for _ in model.stream(PreparedPage("", [], [b"png"], []), "task", {}):
            pass
    assert error.value.code == code
    await model.close()


async def test_large_page_and_image_only_pdf(root, tmp_path):
    """Ограничение pixels применяется до raster allocation; скан идёт в vision."""
    path = tmp_path / "image.pdf"
    with pymupdf.open() as document:
        page = document.new_page(width=3000, height=2000)
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
        pixmap.clear_with(255)
        page.insert_image(page.rect, stream=pixmap.tobytes("png"))
        document.save(path)
    result = await root.pdf.prepare(str(path), 1)
    assert len(result.images) == 3 and any(w.startswith("image_only") for w in result.warnings)
    for image in result.images:
        pixmap = pymupdf.Pixmap(image)
        assert pixmap.width * pixmap.height <= root.settings.max_image_pixels + 10000


def test_excel_formula_injection_does_not_change_value():
    """Строка =... сохраняется как текст, значение совпадает с JSON без добавленного апострофа."""
    export = Export(
        items=[
            SpecificationItem(
                Caption='=HYPERLINK("evil")',
                Units="шт",
                Quantity="001,50",
                UserComment1="P",
                Pages="1",
            )
        ]
    )
    sheet = load_workbook(BytesIO(build_excel(export, [])), data_only=False)["Спецификация"]
    assert sheet["A2"].value == export.items[0].Caption and sheet["A2"].data_type == "s"
    assert sheet["C2"].value == "001,50"


def test_layered_settings(tmp_path):
    """Новая non-secret настройка берётся из каталога; sparse .env меняет только секрет."""
    example, private = tmp_path / ".env.example", tmp_path / ".env"
    example.write_text("APP_PORT=8083\nRENDER_DPI=180\nDATABASE_PASSWORD=\n", encoding="utf8")
    private.write_text("DATABASE_PASSWORD=private-test-password\n", encoding="utf8")
    cfg = Settings(_env_file=(example, private))
    assert cfg.render_dpi == 180 and cfg.app_port == 8083
    assert cfg.database_password.get_secret_value() == "private-test-password"
