"""`gc-cli config …`: точка входа уводит подкоманду в gc_core.config_command
до старта агента, а парсер агента её не трогает (он разбирает argv на импорте)."""

import importlib
import json
import sys

import pytest

# `import cli` парсит sys.argv на импорте — пин валидного argv (тот же приём,
# что в test_cli_broken_config_wiring.py).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import cli
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
def reload_arg_parser(monkeypatch):
    """Перезагрузка arg_parser с нужным argv; после теста — обратно на `-q x`,
    чтобы модуль не остался в состоянии команды config для соседних тестов."""
    from gc_cli import arg_parser as ap_mod

    def _reload(argv):
        monkeypatch.setattr(sys, "argv", argv)
        importlib.reload(ap_mod)
        return ap_mod

    yield _reload
    sys.argv = ["gc-cli", "-q", "x"]
    importlib.reload(ap_mod)


def _no_agent(monkeypatch):
    async def _boom():
        raise AssertionError("config не должен запускать агента")

    monkeypatch.setattr(cli, "arun_cli", _boom)


def test_arg_parser_skips_config_command(reload_arg_parser):
    ap = reload_arg_parser(["gc-cli", "config", "explain", "--all"])
    assert ap.CONFIG_COMMAND is True
    ap = reload_arg_parser(["gc-cli", "-q", "привет"])
    assert ap.CONFIG_COMMAND is False
    assert ap.run_args.query == "привет"


def test_arg_parser_still_requires_query(reload_arg_parser):
    with pytest.raises(SystemExit):
        reload_arg_parser(["gc-cli", "--verbose"])


@pytest.mark.usefixtures("isolated")
def test_gc_cli_config_validate(monkeypatch, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("agent:\n  max_steps: 5\n", encoding="utf-8")
    _no_agent(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["gc-cli", "config", "validate", "--config", str(base)]
    )
    assert cli.main() == 0
    assert capsys.readouterr().out.startswith("ok: ")


@pytest.mark.usefixtures("isolated")
def test_gc_cli_config_validate_broken(monkeypatch, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("agent:\n  max_steps: abc\n", encoding="utf-8")
    _no_agent(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["gc-cli", "config", "validate", "--config", str(base)]
    )
    assert cli.main() == 1
    assert "agent.max_steps" in capsys.readouterr().err


@pytest.mark.usefixtures("isolated")
def test_gc_cli_config_explain_json(monkeypatch, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("agent:\n  max_steps: 5\n", encoding="utf-8")
    _no_agent(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gc-cli",
            "config",
            "explain",
            "--config",
            str(base),
            "--json",
            "max_steps",
        ],
    )
    assert cli.main() == 0
    document = json.loads(capsys.readouterr().out)
    [entry] = document["settings"]
    assert entry["key"] == "agent.max_steps" and entry["value"] == 5
    assert entry["sources"][0]["layer"] == "base"


def test_gc_cli_config_help_lists_subcommands(monkeypatch, capsys):
    _no_agent(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["gc-cli", "config", "--help"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    assert "gc-cli config" in out
    for name in ("explain", "validate", "schema"):
        assert name in out


@pytest.mark.usefixtures("isolated")
def test_gc_cli_config_unknown_key_exits_2(monkeypatch, tmp_path, capsys):
    base = tmp_path / "config.yml"
    base.write_text("{}\n", encoding="utf-8")
    _no_agent(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gc-cli",
            "config",
            "explain",
            "--config",
            str(base),
            "agent.max_stepz",
        ],
    )
    assert cli.main() == 2
    assert "agent.max_steps" in capsys.readouterr().err
