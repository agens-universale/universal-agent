"""`gc-ua config …`: подкоманда уходит в gc_core.config_command ДО TUI —
без textual, агента, MCP и сети."""

import json

import pytest
from gc_core.config_modules import env_vars
from gc_core.config_modules.env_vars import EnvVars


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(env_vars, "env", EnvVars(gc_home=tmp_path / "gc_home"))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project


@pytest.fixture
def no_tui(monkeypatch):
    """Импорт TUI под запретом: команда config обязана обойтись без него."""
    import gc_tui

    real = gc_tui.importlib.import_module

    def _guard(name, *args, **kwargs):
        if name.startswith("gc_tui.app"):
            raise AssertionError("config не должен поднимать TUI")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(gc_tui.importlib, "import_module", _guard)
    return gc_tui


@pytest.mark.usefixtures("isolated")
def test_gc_ua_config_validate(no_tui, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("agent:\n  max_steps: 5\n", encoding="utf-8")
    assert no_tui.main(["config", "validate", "--config", str(base)]) == 0
    assert capsys.readouterr().out.startswith("ok: ")


@pytest.mark.usefixtures("isolated")
def test_gc_ua_config_validate_broken(no_tui, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("agent:\n  max_steps: abc\n", encoding="utf-8")
    assert no_tui.main(["config", "validate", "--config", str(base)]) == 1
    assert "agent.max_steps" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated")
def test_gc_ua_config_explain(no_tui, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("agent:\n  max_steps: 5\n", encoding="utf-8")
    code = no_tui.main(["config", "explain", "--config", str(base), "agent"])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("Конфиг gc-ua")
    assert "agent.max_steps = 5" in out


def test_gc_ua_config_schema(no_tui, capsys):
    assert no_tui.main(["config", "schema"]) == 0
    assert "$defs" in json.loads(capsys.readouterr().out)


def test_gc_ua_config_usage_error(no_tui, capsys):
    assert no_tui.main(["config", "explain", "--bogus"]) == 2
    assert "gc-ua config" in capsys.readouterr().err
