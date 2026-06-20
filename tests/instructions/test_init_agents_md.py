import types
from pathlib import Path
from instructions.init_agents_md import run_init_agents_md, resolve_init_mode


def _ctx(confirm=True):
    return types.SimpleNamespace(is_tui=False, config_dict={},
                                 tui_confirm=None, _force_confirm=confirm)


def test_fresh_writes_file(tmp_path):
    ctx = _ctx()
    res = run_init_agents_md(cwd=str(tmp_path), ctx=ctx,
                             generate_fn=lambda prompt: "# AGENTS\nGENERATED")
    p = Path(tmp_path) / "AGENTS.md"
    assert p.read_text(encoding="utf-8") == "# AGENTS\nGENERATED"
    assert res["status"] == "written" and res["mode"] == "fresh"


def test_existing_cancel_keeps_file(tmp_path, monkeypatch):
    (Path(tmp_path) / "AGENTS.md").write_text("ORIGINAL", encoding="utf-8")
    monkeypatch.setattr("instructions.init_agents_md.resolve_init_mode", lambda **k: "cancel")
    ctx = _ctx()
    res = run_init_agents_md(cwd=str(tmp_path), ctx=ctx, generate_fn=lambda p: "NEW")
    assert (Path(tmp_path) / "AGENTS.md").read_text(encoding="utf-8") == "ORIGINAL"
    assert res["status"] == "cancelled"


def test_update_mode_feeds_existing_into_prompt(tmp_path, monkeypatch):
    (Path(tmp_path) / "AGENTS.md").write_text("OLDBODY", encoding="utf-8")
    monkeypatch.setattr("instructions.init_agents_md.resolve_init_mode", lambda **k: "update")
    seen = {}
    def gen(prompt):
        seen["prompt"] = prompt
        return "MERGED"
    run_init_agents_md(cwd=str(tmp_path), ctx=_ctx(), generate_fn=gen)
    assert "OLDBODY" in seen["prompt"]
