import sys

import pytest

from computer_mcp.core import dpi


def test_setup_is_noop_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    # Must not raise
    dpi.setup_dpi_awareness()


@pytest.mark.windows
def test_setup_on_windows_does_not_raise():
    dpi.setup_dpi_awareness()
