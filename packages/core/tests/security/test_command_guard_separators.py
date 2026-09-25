r"""Tripwire защищённых путей не обманывается ни разделителями, ни кавычками.

Найдено ручным прогоном 18.08.2026: агент попросили удалить `.gc`, он выполнил

    rm -rf .gc; echo "exit=$?"; ls -la

и каталог был удалён. Та же команда через `&&` блокировалась. Причина в
токенизации: `shlex.split` не считает `;` разделителем, поэтому получался токен
`.gc;` — с точкой с запятой внутри, и проверка пути его не узнавала.

Обход агент нашёл не адверсарно: он просто хотел увидеть код возврата.

Починка первого захода — резать сырую строку по `[;&|\n\r]+` ДО `shlex` — рвала
кавычки и завела две новые поломки, которые здесь тоже закреплены:

* ложный ЗАПРЕТ: в `git commit -m "fix: .gc; cleanup"` точка с запятой внутри
  кавычек рвала строку, обе половины падали в грубый фолбэк, и голый `.gc`
  читался как защищённый путь;
* ложный ПРОПУСК: в `rm -rf ".gc" "note; done"` разделитель в соседнем
  кавыченном аргументе ломал кавычки, и защищённый путь оставался токеном
  `".gc"` вместе с кавычками.

Плюс унаследованная дыра: `>` в список разделителей не входил, и
`rm -rf .gc>/dev/null` проходила насквозь. По той же причине не ловились
`(cd sub && rm -rf .gc)` и `rm -rf $(echo .gc)` — скобка прилипала к пути.

Третий блок ниже закрепляет обратную сторону той же поломки: `.gc/skills` —
объявленное исключение, но старая резка склеивала его с перенаправлением
(`.gc/skills>/dev/null`), путь переставал совпадать с исключением и попадал
под общий запрет `.gc`. Исключение было живо только в самых простых формах.
"""

from __future__ import annotations

import pytest
from gc_core.security.command_guard import scan_command_for_protected


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / ".gc").mkdir()
    (tmp_path / ".gc" / "skills").mkdir()
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
        # Кавычки вокруг защищённого пути: раньше токен приезжал как '".gc"'
        # вместе с кавычками и классификатор его не узнавал.
        'rm -rf ".gc" "note; done"',
        "rm -rf '.gc'",
        # Перенаправление вплотную: '>' в список разделителей не входил.
        "rm -rf .gc>/dev/null",
        "rm -rf .gc</dev/null",
        "rm -rf .gc >/dev/null 2>&1",
        "rm -rf .gc>>log 2>&1",
        # Группировка и подстановка: скобка прилипала к пути ('.gc)').
        "(cd sub && rm -rf .gc)",
        "rm -rf $(echo .gc)",
        # Незакрытая кавычка → грубый фолбэк. Такая команда всё равно не
        # выполнится (backend запускает через shell=True), но отказ должен быть
        # осознанным: фолбэк снимает кавычки и экранирование, а не пропускает.
        'rm -rf "unclosed .gc',
        r'rm -rf ./.gc\/x "unclosed',
    ],
)
def test_protected_path_denied_regardless_of_separator(workspace, command):
    assert scan_command_for_protected(command, cwd=workspace) == "deny", (
        "разделитель команд не должен прятать защищённый путь от tripwire"
    )


@pytest.mark.parametrize(
    "command",
    [
        "echo safe; ls -la",
        "ls -la && pwd",
        "cat notes.txt | head",
        # A4: ';' внутри кавычек рвал строку, и голый '.gc' читался как путь.
        'git commit -m "fix: .gc; cleanup"',
        'git commit -m "см. .gc/state; там лог"',
        'git commit -m "fix #1: обновил .gc-доки"',
        "git commit -m fix#1",
        # Решётка — не комментарий: при штатном shlex.commenters слово
        # обрывается и '.gc#backup' даёт голый '.gc' — ложный запрет на
        # удаление постороннего файла.
        "rm -rf .gc#backup",
        "rm -rf .gc#1/state",
        # Двоеточие и запятая обязаны оставаться внутри слова: без
        # whitespace_split они выпадают и '.gc:old' тоже даёт голый '.gc'.
        "rm -rf .gc:old",
        "rm -rf a,.gc",
        "rm -rf ./.gc:tag",
        'echo "a; .gc"',
        'cat "my notes/file.txt"',
        # Похоже на защищённый путь, но им не является: ужесточение не должно
        # растекаться на соседей по имени.
        "rm -rf .gcache>/dev/null",
        "rm -rf my.gc.bak",
        "(cd sub && rm -rf build)",
    ],
)
def test_harmless_commands_still_allowed(workspace, command):
    """Текст в кавычках — аргумент, а не путь: tripwire не ловит лишнее."""
    assert scan_command_for_protected(command, cwd=workspace) == "allow"


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf .gc/skills",
        "rm -rf .gc/skills>/dev/null",
        "rm -rf .gc/skills>>log 2>&1",
        "(cd sub && rm -rf .gc/skills)",
        "rm -rf $(echo .gc/skills)",
        'rm -rf ".gc/skills" "note; done"',
    ],
)
def test_skills_exception_survives_decorations(workspace, command):
    """`.gc/skills` — объявленное исключение, и оно должно переживать оформление.

    Старая резка склеивала путь с перенаправлением и скобками
    (`.gc/skills>/dev/null`), совпадение с исключением терялось, и путь падал
    под общий запрет `.gc`. То есть исключение работало только в голой форме —
    запрет был не строже, а случайнее.
    """
    assert scan_command_for_protected(command, cwd=workspace) == "allow"
