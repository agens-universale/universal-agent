from startup_notices import Notice
from tui.services.notices_ui import (
    format_notices_badge,
    format_notices_footer_suffix,
    notice_counts,
)


def test_notice_counts_and_badge():
    notices = [Notice("warning", "a", "x"), Notice("info", "b", "y")]
    assert notice_counts(notices) == (1, 1)
    assert format_notices_badge(notices) == "⚠ 1 · ℹ 1 · /notices (F4)"
    assert format_notices_footer_suffix(notices) == "  ·  ⚠ 1 · ℹ 1 · /notices (F4)"
    assert format_notices_badge([]) == ""
