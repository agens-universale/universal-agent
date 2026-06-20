# tests/instructions/test_loader.py
from instructions.loader import build_instructions


def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_merges_broad_to_narrow_with_markers(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    l0 = tmp_path / "demo.md"
    _write(l0, "L0BODY")
    _write((tmp_path / "gc") / "GIGACODE.md", "L1BODY")
    _write(proj / "AGENTS.md", "L2BODY")
    _write(proj / ".gc" / "GIGACODE.md", "L3BODY")

    res = build_instructions(agent_md_path=str(l0), cwd=str(proj))

    assert res.loaded_ids == ["L0", "L1", "L2", "L3"]
    # порядок прозы broad→narrow
    assert res.text.index("L0BODY") < res.text.index("L1BODY") < res.text.index("L2BODY") < res.text.index("L3BODY")
    # английский каркас
    assert "Precedence: when instructions conflict" in res.text
    assert "## Project instructions (AGENTS.md)" in res.text


def test_overwrite_drops_L0_L1(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    l0 = tmp_path / "demo.md"
    _write(l0, "L0BODY")
    _write((tmp_path / "gc") / "GIGACODE.md", "L1BODY")
    _write(proj / "AGENTS.md", "L2BODY")

    res = build_instructions(agent_md_path=str(l0), cwd=str(proj), overwrite=True)

    assert res.loaded_ids == ["L2"]
    assert "L0BODY" not in res.text and "L1BODY" not in res.text
    assert "L2BODY" in res.text


def test_missing_layers_produce_no_sections(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    res = build_instructions(agent_md_path=None, cwd=str(proj))
    assert res.loaded_ids == []
    assert res.text == ""
