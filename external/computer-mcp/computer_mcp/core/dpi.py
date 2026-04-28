"""Windows DPI awareness init. Call once at process start."""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def setup_dpi_awareness() -> None:
    """Enable PER_MONITOR_DPI_AWARE on Windows; no-op elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception as e:  # noqa: BLE001
        logger.debug("shcore.SetProcessDpiAwareness failed: %s", e)

    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception as e:  # noqa: BLE001
        logger.warning("DPI awareness could not be set: %s", e)
