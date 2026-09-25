"""Diff-preview для trace: visual_diff → первые 3 строки (не tool-output)."""

from __future__ import annotations

from gc_core.config_modules import env_vars
from gc_tui.diff_tui.service import DiffService
from gc_tui.diff_tui.store import DiffStore


def test_visual_diff_provides_preview_lines_for_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    target = root / "src" / "foo.py"
    target.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text(
        "line1\nCHANGED\nline3\nline4\nextra\n", encoding="utf-8"
    )

    svc = DiffService(
        str(root),
        preview_context_lines=1,
        max_diff_chars=8000,
        emit_notices=False,
    )
    body = svc.visual_diff(str(target))
    assert body.strip()
    all_lines = [ln for ln in body.splitlines() if ln]
    head = all_lines[:3]
    remaining = max(0, len(all_lines) - 3)
    assert len(head) == 3
    assert remaining >= 0
    assert any("CHANGED" in ln or "-" in ln or "+" in ln for ln in all_lines)


def test_created_file_preview_shows_content_not_headers(tmp_path, monkeypatch):
    """Trace-preview для created: +/- содержимое, не ---/+++/@@."""
    from gc_tui.diff_tui.visual import preview_change_lines

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    target = root / "src" / "new.py"
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    svc = DiffService(str(root), emit_notices=False)
    body = svc.visual_diff("src/new.py")
    head, remaining = preview_change_lines(body, limit=3)
    assert head == ["+alpha", "+beta", "+gamma"]
    assert remaining == 0
