"""gc-cli собирает снимок набора плагинов после своих оверрайдов (§8.3)."""

import asyncio
import sys
from types import SimpleNamespace

# `import cli` парсит sys.argv при импорте (--query обязателен).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import arg_parser, cli
from gc_core.settings import runtime_of
from gc_testkit.plugin_store import install_plugin
from gc_testkit.settings import make_settings

_DISABLED = "Плагины выключены: задан paths.skills_data_path"


def _cli_settings(monkeypatch, argv, base=None):
    monkeypatch.setattr(
        cli, "load_settings", lambda *_a, **_k: make_settings(base or {})
    )
    monkeypatch.setattr(
        arg_parser,
        "extra_config_overrides",
        arg_parser._parse_unknown_args(argv),
    )
    loaded, code = asyncio.run(cli._aload_cli_settings(None, None))
    assert code is None
    return loaded


def _names(loaded):
    return tuple(p.name for p in runtime_of(loaded).plugins.plugins)


def test_cli_snapshot_lists_installed(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)

    assert _names(_cli_settings(monkeypatch, [])) == ("inst",)


def test_cli_flag_use_installed_false_drops_installed(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)

    loaded = _cli_settings(monkeypatch, ["--plugins.use_installed", "false"])

    assert runtime_of(loaded).plugins.plugins == ()


def test_cli_prints_snapshot_notices_to_stderr(tmp_path, monkeypatch, capsys):
    loaded = _cli_settings(
        monkeypatch,
        [],
        {
            "paths.skills_data_path": str(tmp_path / "skills"),
            "plugins.paths": [str(tmp_path / "p")],
        },
    )

    cli._print_startup_notices(loaded, SimpleNamespace())

    assert capsys.readouterr().err.count(_DISABLED) == 1
