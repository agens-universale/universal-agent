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
    assert cfg["cli_venv_root"] == ""
    assert cfg["agent_dep_dir"] == ""
    assert cfg["extra_mounts"] == []


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
    """Относительный путь к venv → пустое значение (защита от cwd-зависимости)."""
    cfg = build_swarm_config({
        "swarm": {"enabled": True, "cli_venv_root": "./.venv"},
    })
    assert cfg["cli_venv_root"] == ""
