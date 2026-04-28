import time

import pytest

from gc_computer_mcp.errors import ComputerMCPError, ErrorCategory
from gc_computer_mcp.guards import (
    check_blacklist,
    check_bounds,
    readonly_guard,
    safe_tool,
    timeout_guard,
)


def test_readonly_guard_blocks_when_enabled():
    @readonly_guard(enabled=True)
    def click():
        return "ok"
    with pytest.raises(ComputerMCPError) as ex:
        click()
    assert ex.value.category == ErrorCategory.READONLY_VIOLATION


def test_readonly_guard_passes_when_disabled():
    @readonly_guard(enabled=False)
    def click():
        return "ok"
    assert click() == "ok"


def test_blacklist_rejects_ctrl_alt_del():
    @check_blacklist()
    def shortcut(keys):
        return "ok"
    with pytest.raises(ComputerMCPError) as ex:
        shortcut(keys=["ctrl", "alt", "delete"])
    assert ex.value.category == ErrorCategory.BLACKLISTED_KEYS


def test_blacklist_rejects_win_l():
    @check_blacklist()
    def shortcut(keys):
        return "ok"
    with pytest.raises(ComputerMCPError):
        shortcut(keys=["win", "L"])  # case-insensitive


def test_blacklist_allows_normal_shortcut():
    @check_blacklist()
    def shortcut(keys):
        return "ok"
    assert shortcut(keys=["ctrl", "s"]) == "ok"


def test_bounds_check_accepts_valid():
    @check_bounds()
    def click(x, y, screen_w=1920, screen_h=1080):
        return (x, y)
    assert click(100, 200) == (100, 200)


def test_bounds_check_rejects_negative():
    @check_bounds()
    def click(x, y, screen_w=1920, screen_h=1080):
        return (x, y)
    with pytest.raises(ComputerMCPError) as ex:
        click(-1, 100)
    assert ex.value.category == ErrorCategory.OUT_OF_BOUNDS


def test_timeout_guard_triggers():
    @timeout_guard(seconds=0.05)
    def slow():
        time.sleep(0.2)
    with pytest.raises(ComputerMCPError) as ex:
        slow()
    assert ex.value.category == ErrorCategory.TIMEOUT


def test_timeout_guard_allows_fast():
    @timeout_guard(seconds=1.0)
    def fast():
        return "done"
    assert fast() == "done"


def test_safe_tool_wraps_system_exception():
    @safe_tool()
    def boom():
        raise RuntimeError("unexpected OS thing")
    with pytest.raises(ComputerMCPError) as ex:
        boom()
    assert ex.value.category == ErrorCategory.UNEXPECTED


def test_safe_tool_passes_through_computer_mcp_error():
    @safe_tool()
    def known_failure():
        raise ComputerMCPError(ErrorCategory.ELEMENT_NOT_FOUND, "msg")
    with pytest.raises(ComputerMCPError) as ex:
        known_failure()
    assert ex.value.category == ErrorCategory.ELEMENT_NOT_FOUND


def test_bounds_negative_rejected_without_screen_dims():
    @check_bounds()
    def click(x, y):
        return (x, y)
    with pytest.raises(ComputerMCPError) as ex:
        click(-5, 10)
    assert ex.value.category == ErrorCategory.OUT_OF_BOUNDS


def test_bounds_allows_coords_when_screen_dims_absent():
    """Without screen dims, upper-bound check is skipped — only negatives rejected."""
    @check_bounds()
    def click(x, y):
        return (x, y)
    assert click(10000, 10000) == (10000, 10000)


def test_bounds_accepts_last_pixel():
    @check_bounds()
    def click(x, y, screen_w=None, screen_h=None):
        return (x, y)
    assert click(1919, 1079, screen_w=1920, screen_h=1080) == (1919, 1079)


def test_bounds_rejects_exact_edge():
    @check_bounds()
    def click(x, y, screen_w=None, screen_h=None):
        return (x, y)
    with pytest.raises(ComputerMCPError):
        click(1920, 1080, screen_w=1920, screen_h=1080)


def test_timeout_reraises_worker_exception():
    @timeout_guard(seconds=1.0)
    def raises():
        raise ValueError("from worker")
    with pytest.raises(ValueError) as ex:
        raises()
    assert str(ex.value) == "from worker"


def test_blacklist_no_op_when_keys_absent(caplog):
    @check_blacklist()
    def tool_without_keys(x):
        return x * 2
    import logging
    with caplog.at_level(logging.DEBUG, logger="gc_computer_mcp.guards"):
        assert tool_without_keys(3) == 6
    # Pass silently (debug log optional, don't assert on it)
