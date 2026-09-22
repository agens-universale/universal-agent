"""Tests for public API wrappers in mcp_runtime.cli."""

import os
import subprocess
import sys

from recommended_mcp.runtime import cli as mcp


def test_installable_names_from_bundled_manifest():
    """Check that installable_names() returns list of names from bundled manifest."""
    names = mcp.installable_names()
    assert isinstance(names, list)
    assert "office" in names  # из recommended_mcp/config.yml


def test_install_delegates(monkeypatch):
    """Check that install() delegates to _cmd_install with correct arguments."""
    called = {}

    def mock_install(name, config=None):
        called["name"] = name
        called["config"] = config
        return 0

    monkeypatch.setattr(mcp, "_cmd_install", mock_install)
    assert mcp.install("office") == 0
    assert called["name"] == "office"
    assert called["config"] is None


def test_help_command_lists_all_subcommands(capsys):
    """gc-mcp help печатает справку со всеми командами."""
    assert mcp.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "install" in out
    assert "list" in out
    assert "remove" in out


def test_no_command_shows_help(capsys):
    """gc-mcp без аргумента → справка (не argparse-ошибка), код 0."""
    assert mcp.main([]) == 0
    assert "gc-mcp" in capsys.readouterr().out


def _import_probe(code: str) -> str:
    """Выполнить `code` в чистом интерпретаторе с тем же sys.path и вернуть stdout."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def test_importing_cli_does_not_pull_install():
    """Импорт `runtime.cli` не тянет `install` — восемь локальных импортов
    схлопнуты в ленивый `_inst()`, а не подняты наверх.

    Цена подъёма настоящая: `install` тянет `urllib.request` и
    `gc_core.mcp_runtime.state` (~25 мс). Платил бы её и `gc-mcp help`, которому
    install не нужен вовсе, и открытие `/settings` в TUI —
    `gc_tui/services/mcp_manager.py` импортирует этот модуль верхнеуровнево.
    Сетка краснеет на `from recommended_mcp import install` в шапке cli.py.
    """
    out = _import_probe(
        "import sys; import recommended_mcp.runtime.cli; print('recommended_mcp.install' in sys.modules)"
    )
    assert out == "False"


def test_install_does_not_import_cli_back():
    """`install` про `runtime.cli` не знает — иначе `_inst()` был бы циклом.

    Ровно это условие и делает схлопывание восьми импортов законным. Появится
    ребро обратно — лечить снова начнут восемью локальными копиями, поэтому
    условие закреплено, а не оставлено на память.
    """
    out = _import_probe(
        "import sys; import recommended_mcp.install; print('recommended_mcp.runtime.cli' in sys.modules)"
    )
    assert out == "False"
