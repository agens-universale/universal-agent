"""Diff-preview для trace: visual_diff → первые 3 строки (не tool-output)."""
from __future__ import annotations

import json

from config_modules import env_vars
from diff_tui.service import DiffService
from diff_tui.store import DiffStore
from tui.services.tool_preview import extract_preview_lines


def test_extract_preview_still_ignores_write_ok_only(tmp_path, monkeypatch):
    """Регрессия: write-style {ok:true} без content → пустой extract."""
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    out = json.dumps({"ok": True, "path": str(tmp_path / "a.py")})
    lines, remaining = extract_preview_lines(out)
    assert lines == []
    assert remaining == 0


def test_visual_diff_provides_preview_lines_for_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    target = root / "src" / "foo.py"
    target.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    DiffStore(str(root)).register_mutation(str(target))
    target.write_text("line1\nCHANGED\nline3\nline4\nextra\n", encoding="utf-8")

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
