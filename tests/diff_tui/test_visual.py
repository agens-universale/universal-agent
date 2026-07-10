"""visual.py: context lines + truncation (spec2 T14)."""
from __future__ import annotations

from diff_tui.visual import build_unified_diff, truncate_diff


def test_truncate_diff_respects_max_chars():
    text = "x" * 100
    out = truncate_diff(text, max_chars=20)
    assert out.startswith("x" * 20)
    assert "[diff truncated]" in out
    assert len(out) > 20


def test_truncate_diff_zero_means_unlimited():
    text = "y" * 10_000
    assert truncate_diff(text, max_chars=0) == text


def test_build_unified_diff_includes_context_and_hunk():
    old = "a\nb\nc\nd\ne\n"
    new = "a\nb\nX\nd\ne\n"
    out = build_unified_diff(
        old,
        new,
        fromfile="a/f",
        tofile="b/f",
        context_lines=1,
        max_diff_chars=8000,
    )
    assert "--- a/f" in out
    assert "+++ b/f" in out
    assert "-c" in out
    assert "+X" in out


def test_build_unified_diff_truncates(tmp_path):
    old = "\n".join(f"line-{i}" for i in range(200)) + "\n"
    new = "\n".join(f"line-{i}-changed" for i in range(200)) + "\n"
    out = build_unified_diff(
        old, new, context_lines=3, max_diff_chars=80
    )
    assert "[diff truncated]" in out
    assert len(out) <= 80 + len("\n... [diff truncated]\n")


def test_build_created_file_diff_shows_content():
    from diff_tui.visual import build_created_file_diff

    out = build_created_file_diff("hello\n", tofile="new.py")
    assert "--- /dev/null" in out
    assert "+++ new.py" in out
    assert "+hello" in out


def test_build_created_file_diff_empty_file_not_blank():
    from diff_tui.visual import build_created_file_diff

    out = build_created_file_diff("", tofile="empty.txt")
    assert out.strip()
    assert "--- /dev/null" in out
    assert "+++ empty.txt" in out


def test_preview_change_lines_skips_headers_for_created():
    from diff_tui.visual import build_created_file_diff, preview_change_lines

    body = build_created_file_diff("a\nb\nc\n", tofile="f.py")
    head, remaining = preview_change_lines(body, limit=3)
    assert head == ["+a", "+b", "+c"]
    assert remaining == 0
    assert all(not ln.startswith(("---", "+++", "@@")) for ln in head)
