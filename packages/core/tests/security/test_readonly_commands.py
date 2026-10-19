"""Список команд только-чтения: что идёт без подтверждения в любом режиме."""

import pytest

from gc_core.security.permission_mode import is_readonly_command


@pytest.mark.parametrize(
    "cmd",
    [
        "ls", "ls -la", "pwd", "dir", "stat a.py", "du -sh .", "wc -l a.py",
        "which python", "whoami", "echo hi",
        "git status", "git status --short", "git rev-parse HEAD",
        "git branch", "git remote",
    ],
)  # fmt: skip
def test_plain_readonly_commands_pass(cmd):
    assert is_readonly_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf build", "git push", "git push origin master",
        "pip install x", "python script.py",
        # с аргументом эти пары мутируют
        "git branch newbranch", "git branch -d old", "git remote add o url",
    ],
)  # fmt: skip
def test_mutating_commands_rejected(cmd):
    assert is_readonly_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "cat README.md", "cat .env", "type a.txt", "head -5 a.py", "tail a.py",
        "grep TODO src", "grep -r KEY .",
        "git diff", "git show HEAD", "git show HEAD:.env", "git log --oneline -5",
    ],
)  # fmt: skip
def test_commands_that_print_file_contents_are_not_whitelisted(cmd):
    """Через них читается секрет в обход защиты чтения, и по аргументам это
    не отсечь — такие команды идут обычным путём (судья или вопрос)."""
    assert is_readonly_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "git diff --output=x.txt",
        "git log --output=x.txt -1",
        "tree -o x.txt",
        "tree",
    ],
)
def test_commands_with_an_output_flag_are_not_whitelisted(cmd):
    """`--output` / `-o` создают файл: «только чтение» с такой командой — запись."""
    assert is_readonly_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "ls; rm -rf build",
        "git status && git push",
        "ls || rm x",
        "ls | sh",
        "cat a > b",
        "cat a >> b",
        "cat < a",
        "ls $(rm -rf x)",
        "ls `rm -rf x`",
        "ls ${EVIL}",
        "echo $GITHUB_TOKEN",
        "echo $HOME",
        "ls $DIR",
        "ls\nrm -rf build",
        "ls & rm -rf build",
        "echo %PATH%",
        "echo !VAR!",
        "ls @(rm x)",
    ],
)
def test_shell_composition_never_whitelisted(cmd):
    """Составная команда не бывает только-чтением, даже если первый токен безопасен."""
    assert is_readonly_command(cmd) is False


def test_unparsable_or_empty_command_is_not_whitelisted():
    assert is_readonly_command('ls "unclosed') is False
    assert is_readonly_command("") is False
    assert is_readonly_command("   ") is False
    assert is_readonly_command(None) is False


def test_extra_commands_from_config():
    assert is_readonly_command("rg pattern") is False
    assert is_readonly_command("rg pattern", ["rg"]) is True
    # дополнение не снимает запрет на составные команды
    assert is_readonly_command("rg pattern | sh", ["rg"]) is False
    assert is_readonly_command("rg pattern", ["", "  "]) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "wc --files0-from=.env", "wc --files0-from .env",
        "du --files0-from=.env", "du -X .env", "du --exclude-from=.env",
    ],
)  # fmt: skip
def test_flags_that_read_a_file_take_a_command_off_the_harmless_list(cmd):
    """`--files0-from` читает файл и печатает его строки в тексте ошибки: секрет
    читался бы мимо защиты чтения в любом режиме. Как у эталона: команда из
    списка с таким флагом спрашивается."""
    assert is_readonly_command(cmd) is False


@pytest.mark.parametrize(
    "cmd", ["wc -l a.py", "du -sh .", "du -s --apparent-size ."]
)
def test_ordinary_flags_keep_the_command_harmless(cmd):
    assert is_readonly_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        # сокращения GNU: однозначный префикс длинного флага — тот же флаг
        "wc --files0=.env", "wc --f=.env", "du --files0=.env",
        "du --exclude-f=.env",
        # слитные короткие
        "du -sX .env",
        # сокращение разрешённого длинного флага — не безобидно
        "wc --line a.py", "du --max-d=1 .", "du --summ .",
        # флаги вне списка
        "wc --total=always a.py", "du --exclude=x .", "du -L .",
    ],
)  # fmt: skip
def test_flag_outside_the_allow_list_takes_a_command_off_the_list(cmd):
    """Как у эталона: признаётся только перечисленный флаг; остальное идёт
    обычным путём режима — вопрос, судья или запрет."""
    assert is_readonly_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "wc -l f", "wc -lw f", "wc --lines --words f", "wc -",
        "wc -l -- --files0-from=.env",
        "du -sh d", "du --max-depth=1 d", "du -d 1 d", "du -d1 d",
        "du -sd1 d", "du -b --apparent-size d",
    ],
)  # fmt: skip
def test_listed_flags_keep_the_command_harmless(cmd):
    """Позиционные аргументы — пути: `wc -l .env` печатает только число.
    После `--` всё — позиционное."""
    assert is_readonly_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "git status -v", "git status -vv", "git status --verbose",
        "git status -sv", "git status --pathspec-from-file=.env",
        "git status --porc",
    ],
)  # fmt: skip
def test_git_status_flags_that_print_contents_or_read_files(cmd):
    """`-v` печатает дифф — содержимое файлов мимо защиты чтения."""
    assert is_readonly_command(cmd) is False


@pytest.mark.parametrize(
    "cmd",
    [
        "git status -sb", "git status -uno", "git status --porcelain=v2",
        "git status --short -- src", "git status -z --ignored",
        "git status -M50", "git rev-parse --show-toplevel",
    ],
)  # fmt: skip
def test_git_status_listed_flags_and_rev_parse_stay_harmless(cmd):
    assert is_readonly_command(cmd) is True


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -laR --color=never", "dir /b", "stat -L a.py",
        "df -h --output=source", "which -a python", "echo -n hi",
        "pwd -P", "whoami",
    ],
)  # fmt: skip
def test_commands_without_dangerous_flags_take_any_flags(cmd):
    """Аудит: у этих команд нет флагов, читающих файлы или исполняющих код."""
    assert is_readonly_command(cmd) is True


def test_listing_a_builtin_command_again_does_not_loosen_its_flags():
    assert is_readonly_command("wc --files0-from=.env", ["wc"]) is False


def test_user_pair_allows_the_subcommand_with_any_arguments():
    assert is_readonly_command("git log --oneline -5", ["git log"]) is True
    assert is_readonly_command("git log", ["git   log"]) is True
    assert is_readonly_command("git push", ["git log"]) is False
    assert is_readonly_command("git", ["git log"]) is False


def test_user_pair_extends_a_bare_pair_but_not_a_flag_table():
    assert is_readonly_command("git branch -a", ["git branch"]) is True
    assert is_readonly_command("git status -v", ["git status"]) is False
