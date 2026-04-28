"""Vision tools — optional, activated via GC_COMPUTER_MCP_ENABLE_VISION.

**Important:** background-agent currently converts any binary MCP output into
the string "binary data" during sanitisation. That means even a correctly
formatted MCP image content block won't reach the LLM as an image. These tools
are implemented for the day that changes; in v0.1 they are disabled by default.
"""

from __future__ import annotations

import base64
import io
from collections.abc import Callable
from typing import Any

from PIL import Image

from ..errors import ComputerMCPError, ErrorCategory


def _default_png_bytes_provider(region: dict[str, int] | None) -> tuple[bytes, int, int, float]:
    import mss

    with mss.mss() as sct:
        if region is None:
            mon = sct.monitors[1]  # primary
            region = {"left": mon["left"], "top": mon["top"],
                      "width": mon["width"], "height": mon["height"]}
        else:
            region = {"left": region["x"], "top": region["y"],
                      "width": region["width"], "height": region["height"]}
        img = sct.grab(region)
        pil = Image.frombytes("RGB", (img.width, img.height), img.rgb)
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue(), img.width, img.height, 1.0


def capture_screenshot(
    *,
    region: dict[str, int] | None = None,
    downscale: bool = False,
    _png_bytes_provider: Callable = _default_png_bytes_provider,
) -> dict[str, Any]:
    png_bytes, w, h, scale = _png_bytes_provider(region)
    if downscale:
        img = Image.open(io.BytesIO(png_bytes))
        img.thumbnail((1280, 720))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        w, h = img.size
    return {
        "image": base64.b64encode(png_bytes).decode("ascii"),
        "width": w,
        "height": h,
        "scale_factor": scale,
    }


def analyze_screen_region(
    *,
    x: int,
    y: int,
    width: int,
    height: int,
    _png_bytes_provider: Callable = _default_png_bytes_provider,
) -> dict[str, Any]:
    if width <= 0 or height <= 0:
        raise ComputerMCPError(
            ErrorCategory.INVALID_INPUT,
            f"width and height must be positive, got {width}x{height}",
        )
    region = {"x": x, "y": y, "width": width, "height": height}
    png_bytes, w, h, scale = _png_bytes_provider(region)
    return {
        "image": base64.b64encode(png_bytes).decode("ascii"),
        "width": w,
        "height": h,
        "scale_factor": scale,
    }
