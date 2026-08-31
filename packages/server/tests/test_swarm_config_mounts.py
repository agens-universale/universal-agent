"""Тесты для новых полей build_swarm_config: cli_venv_root, agent_dep_dir, extra_mounts.

Шаг M4/1.11 (AR-6, AR-7).
"""

import os
import sys

import pytest


from gc_server.swarm.config import build_swarm_config, _abs_or_empty


# ---------------------------------------------------------------------------
# _abs_or_empty helper
# ---------------------------------------------------------------------------


def test_abs_or_empty_accepts_absolute_path():
    assert _abs_or_empty("/opt/gc-cli/.venv") == "/opt/gc-cli/.venv"


def test_abs_or_empty_strips_whitespace():
    assert _abs_or_empty("  /opt/x  ") == "/opt/x"


def test_abs_or_empty_rejects_relative_path():
    """Защита: относительные пути потенциально опасны (cwd-зависимы)."""
    assert _abs_or_empty("./.venv") == ""
    assert _abs_or_empty("relative/path") == ""


def test_abs_or_empty_rejects_none():
    assert _abs_or_empty(None) == ""


def test_abs_or_empty_rejects_non_string():
    assert _abs_or_empty(123) == ""
    assert _abs_or_empty([]) == ""


def test_abs_or_empty_rejects_empty():
    assert _abs_or_empty("") == ""
    assert _abs_or_empty("   ") == ""


# ---------------------------------------------------------------------------
# build_swarm_config — новые поля mount'ов
# ---------------------------------------------------------------------------


def test_build_swarm_config_new_mount_fields_defaults():
    cfg = build_swarm_config({"swarm": {"enabled": True}})
    assert cfg["agent_dep_dir"] == ""
    assert cfg["extra_mounts"] == []


def test_cli_venv_root_defaults_to_the_venv_the_server_runs_in():
    """Ключ не задан → venv сервера, если gc-cli там есть.

    `uv tool install gc-server` ставит gc-cli зависимостью в venv инструмента,
    но наружу, в PATH, выносит только gc-serve — команды зависимостей uv не
    экспортирует. Пока дефолтом было пустое значение, обёртка уходила в
    PATH-fallback, команду не находила, и КАЖДЫЙ sworm завершался exit 127.

    Подмена, которую ловит тест: убрать подстановку sys.prefix — дефолт снова
    станет пустым, и штатная установка снова перестанет запускать агента.
    """
    cfg = build_swarm_config({"swarm": {"enabled": True}})

    if os.path.isfile(os.path.join(sys.prefix, "bin", "gc-cli")):
        assert cfg["cli_venv_root"] == sys.prefix
    else:
        # gc-cli рядом нет (системный python без venv) — прежний PATH-fallback,
        # а обёртка отказывает на нём внятно, а не голым exit 127.
        assert cfg["cli_venv_root"] == ""


def test_build_swarm_config_with_explicit_cli_venv_root():
    cfg = build_swarm_config({
        "swarm": {"enabled": True, "cli_venv_root": "/opt/gc-cli/.venv"},
    })
    assert cfg["cli_venv_root"] == "/opt/gc-cli/.venv"


def test_build_swarm_config_with_agent_dep_dir():
    cfg = build_swarm_config({
        "swarm": {"enabled": True, "agent_dep_dir": "/opt/gc-cli/mcp-deps"},
    })
    assert cfg["agent_dep_dir"] == "/opt/gc-cli/mcp-deps"


def test_build_swarm_config_with_extra_mounts():
    cfg = build_swarm_config({
        "swarm": {
            "enabled": True,
            "extra_mounts": ["/root/.npm", "/root/.cache/ms-playwright"],
        },
    })
    assert cfg["extra_mounts"] == ["/root/.npm", "/root/.cache/ms-playwright"]


def test_build_swarm_config_extra_mounts_skips_empty():
    cfg = build_swarm_config({
        "swarm": {
            "enabled": True,
            "extra_mounts": ["/a", "", "  ", "/b"],
        },
    })
    assert cfg["extra_mounts"] == ["/a", "/b"]


def test_build_swarm_config_extra_mounts_non_list_falls_back():
    """Если кто-то указал extra_mounts как dict — игнорируем."""
    cfg = build_swarm_config({
        "swarm": {"enabled": True, "extra_mounts": {"oops": "/x"}},
    })
    assert cfg["extra_mounts"] == []


def test_build_swarm_config_rejects_relative_cli_venv_root():
    """Относительный путь к venv → пустое значение (защита от cwd-зависимости).

    Дефолт «venv сервера» здесь НЕ подставляется намеренно: ключ человек
    написал, значит опечатку он должен увидеть как игнорирование, а не как
    тихую подмену на чужой venv. Дефолт срабатывает только когда ключа нет
    вовсе — см. test_cli_venv_root_defaults_to_the_venv_the_server_runs_in.
    """
    cfg = build_swarm_config({
        "swarm": {"enabled": True, "cli_venv_root": "./.venv"},
    })
    assert cfg["cli_venv_root"] == ""
