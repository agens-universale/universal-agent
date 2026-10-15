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
