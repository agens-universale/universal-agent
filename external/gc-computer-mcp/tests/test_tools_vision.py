import base64
from io import BytesIO

import pytest
from PIL import Image

from gc_computer_mcp.errors import ComputerMCPError
from gc_computer_mcp.tools.vision import analyze_screen_region, capture_screenshot


def _make_png_bytes(w=200, h=100):
    img = Image.new("RGB", (w, h), color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_capture_screenshot_returns_base64_png():
    fake_png = _make_png_bytes(100, 50)
    res = capture_screenshot(
        region=None,
        downscale=False,
        _png_bytes_provider=lambda region: (fake_png, 100, 50, 1.0),
    )
    assert "image" in res
    b = base64.b64decode(res["image"])
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    assert res["width"] == 100
    assert res["height"] == 50
    assert res["scale_factor"] == 1.0


def test_analyze_screen_region_uses_given_rect():
    called = {}

    def fake(region):
        called["region"] = region
        return (_make_png_bytes(64, 32), 64, 32, 1.0)

    res = analyze_screen_region(x=10, y=20, width=64, height=32,
                                _png_bytes_provider=fake)
    assert called["region"] == {"x": 10, "y": 20, "width": 64, "height": 32}
    assert res["width"] == 64
    assert res["height"] == 32


def test_analyze_rejects_zero_region():
    with pytest.raises(ComputerMCPError):
        analyze_screen_region(x=0, y=0, width=0, height=10,
                              _png_bytes_provider=lambda r: (b"", 0, 0, 1.0))
