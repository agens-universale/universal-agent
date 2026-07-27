"""visual.py: context lines + truncation (spec2 T14)."""
from __future__ import annotations

from diff_tui.visual import (
    build_unified_diff,
    build_visual_diff,
    truncate_diff,
)


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


# --- build_visual_diff (spec §5, D1–D5) ---


def test_build_visual_diff_adaptive_short_shows_whole_file():
    old = "\n".join(f"old-{i}" for i in range(10)) + "\n"
    new_lines = ["NEW-0"] + [f"old-{i}" for i in range(1, 10)]
    new = "\n".join(new_lines) + "\n"
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=300,
        preview_context_lines=50,
        max_diff_chars=100_000,
    )
    # весь файл (10 строк) виден: far-строка old-9 в контексте
    assert "old-9" in out
    assert "-old-0" in out
    assert "+NEW-0" in out


def test_build_visual_diff_adaptive_long_uses_preview_context():
    old = "\n".join(f"line-{i}" for i in range(400)) + "\n"
    new_lines = ["line-0", "CHANGED"] + [f"line-{i}" for i in range(2, 400)]
    new = "\n".join(new_lines) + "\n"
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=300,
        preview_context_lines=2,
        max_diff_chars=200_000,
    )
    assert "+CHANGED" in out
    assert "line-399" not in out  # far от изменения
    assert "line-3" in out  # в пределах n=2 от изменения


def test_build_visual_diff_full_shows_whole_file_even_if_long():
    old = "\n".join(f"line-{i}" for i in range(400)) + "\n"
    new_lines = ["line-0", "CHANGED"] + [f"line-{i}" for i in range(2, 400)]
    new = "\n".join(new_lines) + "\n"
    out = build_visual_diff(
        old,
        new,
        mode="full",
        full_file_max_lines=300,
        preview_context_lines=50,
        max_diff_chars=500_000,
    )
    assert "line-399" in out
    assert "+CHANGED" in out


def test_build_visual_diff_adaptive_boundary_equal_full_file_max_lines():
    n_lines = 5
    old = "\n".join(f"o{i}" for i in range(n_lines)) + "\n"
    new = "\n".join(["o0", "CHANGED", "o2", "o3", "o4"]) + "\n"
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=n_lines,
        preview_context_lines=1,
        max_diff_chars=100_000,
    )
    assert "o4" in out  # весь файл виден на границе
    assert "+CHANGED" in out


def test_build_visual_diff_adaptive_boundary_one_more_uses_preview():
    n_lines = 6  # full_file_max_lines=5 → preview
    old = "\n".join(f"o{i}" for i in range(n_lines)) + "\n"
    new = (
        "\n".join(["o0", "CHANGED"] + [f"o{i}" for i in range(2, n_lines)]) + "\n"
    )
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=5,
        preview_context_lines=1,
        max_diff_chars=100_000,
    )
    assert "+CHANGED" in out
    assert "o5" not in out  # за пределами n=1 от изменения


def test_build_visual_diff_invalid_mode_raises():
    import pytest

    with pytest.raises(ValueError):
        build_visual_diff("a\n", "b\n", mode="side")


def test_build_visual_diff_truncates():
    old = "\n".join(f"o{i}" for i in range(50)) + "\n"
    new = "\n".join([f"o{i}-c" for i in range(50)]) + "\n"
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=300,
        preview_context_lines=50,
        max_diff_chars=50,
    )
    assert "[diff truncated]" in out


def test_build_visual_diff_defaults_adaptive():
    # режим по умолчанию = adaptive
    old = "a\nb\nc\n"
    new = "a\nB\nc\n"
    out = build_visual_diff(old, new)
    assert "-b" in out
    assert "+B" in out


# --- build_visual_diff: hunk merge (spec §5) ---


def test_build_visual_diff_adaptive_close_edits_merge_into_one_hunk():
    """Длинный файл + два близких edit (в пределах n=50) → один hunk.

    Спека §5: пересекающиеся контексты склеивает ``difflib`` сам; регрессия
    по merge должна ловиться (remark 2).
    """
    old = "".join(f"line-{i}\n" for i in range(400))
    # два изменения в строках 10 и 20 — контексты (n=50) пересекаются
    new_lines = list(f"line-{i}\n" for i in range(400))
    new_lines[10] = "EDIT-A\n"
    new_lines[20] = "EDIT-B\n"
    new = "".join(new_lines)
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=300,
        preview_context_lines=50,
        max_diff_chars=500_000,
    )
    n_hunks = out.count("\n@@")
    assert n_hunks == 1, f"близкие edit должны склеиться в 1 hunk, got {n_hunks}"
    assert "+EDIT-A" in out
    assert "+EDIT-B" in out


def test_build_visual_diff_adaptive_far_edits_stay_two_hunks():
    """Длинный файл + два далёких edit (>2n) → два отдельных hunk."""
    old = "".join(f"line-{i}\n" for i in range(400))
    new_lines = list(f"line-{i}\n" for i in range(400))
    new_lines[10] = "EDIT-A\n"
    new_lines[300] = "EDIT-B\n"  # далеко от 10: 300-10=290 >> 2*50
    new = "".join(new_lines)
    out = build_visual_diff(
        old,
        new,
        mode="adaptive",
        full_file_max_lines=300,
        preview_context_lines=50,
        max_diff_chars=500_000,
    )
    n_hunks = out.count("\n@@")
    assert n_hunks == 2, f"далёкие edit должны дать 2 hunk, got {n_hunks}"
    assert "+EDIT-A" in out
    assert "+EDIT-B" in out
