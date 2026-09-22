import yaml
from gc_core.config_modules import env_vars


def _gc(tmp_path):
    return {"config": tmp_path / "cfg", "data": tmp_path / "d", "cache": tmp_path / "cache", "state": tmp_path / "s"}


def test_resolve_placeholders():
    from recommended_mcp.runtime.wiring import resolve_placeholders

    block = {"command": "{npx}", "args": ["-y", "{cache}/x"], "transport": "stdio"}
    out = resolve_placeholders(block, npx="/n/npx", cache="/c")
    assert out == {"command": "/n/npx", "args": ["-y", "/c/x"], "transport": "stdio"}


def test_wire_and_unwire(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "gc_paths", lambda: _gc(tmp_path))
    # фейковый recipe-каталог со сниппетом
    recipe = tmp_path / "rec" / "office"
    recipe.mkdir(parents=True)
    (recipe / "agent_config.snippet.yml").write_text(
        "office:\n  transport: stdio\n  command: office-mcp\n  server-name: office\n",
        encoding="utf-8",
    )
    from recommended_mcp.runtime import wiring

    wiring.wire_snippet("office", recipe, npx="/n/npx", cache="/c")

    managed = yaml.safe_load((tmp_path / "cfg" / "config.local.yml").read_text(encoding="utf-8"))
    assert managed["mcp"]["servers"]["office"]["command"] == "office-mcp"
    assert wiring.wired_servers() == {"office": managed["mcp"]["servers"]["office"]}

    assert wiring.unwire("office") is True
    assert wiring.wired_servers() == {}
    assert wiring.unwire("office") is False  # уже нет — no-op


def test_wire_snippet_rejects_multi_key(tmp_path, monkeypatch):
    monkeypatch.setattr(env_vars, "gc_paths", lambda: _gc(tmp_path))
    recipe = tmp_path / "rec" / "bad"
    recipe.mkdir(parents=True)
    (recipe / "agent_config.snippet.yml").write_text(
        "a:\n  transport: stdio\nb:\n  transport: stdio\n", encoding="utf-8"
    )
    import pytest
    from recommended_mcp.runtime import wiring

    with pytest.raises(ValueError):
        wiring.wire_snippet("bad", recipe, npx="/n", cache="/c")


def test_wire_playwright_resolves_real_paths(tmp_path, monkeypatch):
    from gc_core.config_modules import env_vars

    monkeypatch.setattr(
        env_vars,
        "gc_paths",
        lambda: {
            "config": tmp_path / "cfg",
            "data": tmp_path / "d",
            "cache": tmp_path / "cache",
            "state": tmp_path / "s",
        },
    )
    # Путь от корня репозитория, а не от cwd: набор ядра гоняется и из
    # packages/core, где относительный путь никуда не ведёт.
    from gc_testkit.paths import RECIPES
    from recommended_mcp.runtime import wiring

    recipe = RECIPES / "browser_playwright"
    leftover = wiring.wire_snippet("browser_playwright", recipe, npx="/N/npx", cache="/C")
    srv = wiring.wired_servers()["browser_playwright"]
    assert srv["command"] == "/N/npx"
    assert "/C/playwright-mcp" in " ".join(srv["args"])
    assert leftover == []  # без хардкода и оставшихся {…}
