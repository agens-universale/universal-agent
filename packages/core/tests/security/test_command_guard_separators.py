"""Tripwire защищённых путей не обманывается разделителями команд.

Найдено ручным прогоном 18.08.2026: агент попросили удалить `.gc`, он выполнил

    rm -rf .gc; echo "exit=$?"; ls -la

и каталог был удалён. Та же команда через `&&` блокировалась. Причина в
токенизации: `shlex.split` не считает `;` разделителем, поэтому получался токен
`.gc;` — с точкой с запятой внутри, и проверка пути его не узнавала.

Обход агент нашёл не адверсарно: он просто хотел увидеть код возврата.
"""

from __future__ import annotations

import os

import pytest

from gc_core.security.command_guard import scan_command_for_protected


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / ".gc").mkdir()
    return str(tmp_path)


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .gc",
        "rm -rf .gc && ls -la",
        'rm -rf .gc; echo "exit=$?"; ls -la',
        "rm -rf .gc | tee log",
        "ls -la; rm -rf .gc",
        "true & rm -rf .gc",
        "ls -la\nrm -rf .gc",
        "rm -rf ./.gc",
    ],
)
def test_protected_path_denied_regardless_of_separator(workspace, command):
    assert scan_command_for_protected(command, cwd=workspace) == "deny", (
        "разделитель команд не должен прятать защищённый путь от tripwire"
    )


@pytest.mark.parametrize(
    "command",
    ["echo safe; ls -la", "ls -la && pwd", "cat notes.txt | head"],
)
def test_harmless_commands_still_allowed(workspace, command):
    """Резка по разделителям не должна ловить лишнее."""
    assert scan_command_for_protected(command, cwd=workspace) == "allow"
