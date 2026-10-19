"""Секция `security.permissions`: схема и шаблоны."""

import pytest
from gc_testkit.settings import make_settings
from pydantic import ValidationError

from gc_core.config_modules.root import AppConfig
from gc_core.security.permission_mode import (
    AVAILABLE_MODES,
    COMMAND_LAUNCHERS,
    INTERPRETER_COMMANDS,
    RUNNER_TOOLS,
)
from gc_core.settings import is_explicit


def test_permissions_defaults():
    permissions = AppConfig().security.permissions
    assert permissions.default_mode == "edit"
    assert permissions.readonly_commands == []


def test_permissions_from_config():
    cfg = AppConfig.model_validate(
        {
            "security": {
                "permissions": {
                    "default_mode": "manual",
                    "readonly_commands": ["rg"],
                }
            }
        }
    )
    assert cfg.security.permissions.default_mode == "manual"
    assert cfg.security.permissions.readonly_commands == ["rg"]


def test_mode_value_is_normalised():
    cfg = AppConfig.model_validate(
        {"security": {"permissions": {"default_mode": " Manual "}}}
    )
    assert cfg.security.permissions.default_mode == "manual"


@pytest.mark.parametrize("value", ["турбо", "accept_edits", "bypass", ""])
def test_unknown_mode_rejected(value):
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"security": {"permissions": {"default_mode": value}}}
        )


def test_every_available_mode_accepted_by_schema():
    for mode in AVAILABLE_MODES:
        cfg = AppConfig.model_validate(
            {"security": {"permissions": {"default_mode": mode.value}}}
        )
        assert cfg.security.permissions.default_mode == mode.value
    assert {m.value for m in AVAILABLE_MODES} == {
        "edit",
        "manual",
        "plan",
        "auto",
    }


def test_schema_gate_follows_available_modes(monkeypatch):
    """Валидатор отсекает режим, которого нет в сборке: сужаем кортеж и
    проверяем сам гейт — тест переживёт появление новых режимов."""
    import gc_core.security.permission_mode as pm

    monkeypatch.setattr(pm, "AVAILABLE_MODES", (pm.Mode.EDIT,))
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"security": {"permissions": {"default_mode": "manual"}}}
        )


def test_default_mode_is_not_explicit_until_the_user_sets_it():
    assert (
        is_explicit(AppConfig(), "security.permissions.default_mode") is False
    )
    loaded = make_settings({"security.permissions.default_mode": "manual"})
    assert (
        is_explicit(loaded.settings, "security.permissions.default_mode")
        is True
    )


def _with_readonly(entries):
    return AppConfig.model_validate(
        {"security": {"permissions": {"readonly_commands": entries}}}
    )


def test_readonly_pair_and_plain_command_are_accepted():
    cfg = _with_readonly(["git log", "tree"])
    assert cfg.security.permissions.readonly_commands == ["git log", "tree"]


def test_single_word_command_with_subcommands_is_rejected():
    """`git` одним словом — это и `git push`: нужна подкоманда."""
    with pytest.raises(ValidationError, match="git log"):
        _with_readonly(["git"])


@pytest.mark.parametrize("name", sorted(INTERPRETER_COMMANDS))
def test_interpreter_cannot_be_a_harmless_command(name):
    with pytest.raises(ValidationError, match="интерпретатор"):
        _with_readonly(["tree", name])


@pytest.mark.parametrize("entry", ["bash -c", "python -m", "env FOO=1"])
def test_interpreter_as_the_first_word_of_a_pair_is_rejected(entry):
    with pytest.raises(ValidationError, match="интерпретатор"):
        _with_readonly([entry])


@pytest.mark.parametrize(
    "entry",
    ["python3.12", "/usr/bin/python", "C:\\Tools\\pwsh.exe", "node18 x"],
)
def test_interpreter_is_recognised_without_path_and_version(entry):
    """Путь, хвост версии и `.exe` интерпретатор не прячут."""
    with pytest.raises(ValidationError, match="интерпретатор"):
        _with_readonly([entry])


@pytest.mark.parametrize(
    "entry", ["sudo", "xargs", "npx", "uvx", "timeout", "/usr/bin/nohup"]
)
def test_command_that_runs_another_command_is_rejected(entry):
    """Запускатель исполнит любую команду, переданную ему аргументом."""
    with pytest.raises(ValidationError, match="запускает другую команду"):
        _with_readonly([entry])


@pytest.mark.parametrize("entry", ["/usr/bin/git", "git.exe", "GIT"])
def test_command_with_subcommands_is_recognised_without_path(entry):
    """Путь, `.exe` и регистр не прячут команду с подкомандами."""
    with pytest.raises(ValidationError, match="команда с подкомандами"):
        _with_readonly([entry])


@pytest.mark.parametrize(
    "entry",
    [
        "PWSH.EXE",
        "Python.exe",
        "CMD",
        "python3.13t",
        "python3.12-dbg",
        "pythonw",
        "pypy3",
        "ipython",
        "py",
    ],
)
def test_interpreter_hidden_by_case_suffix_or_variant_is_rejected(entry):
    with pytest.raises(ValidationError, match="интерпретатор"):
        _with_readonly([entry])


@pytest.mark.parametrize(
    "entry",
    ["stdbuf", "setsid", "ionice", "busybox", "ssh", "pkexec", "wsl"],
)
def test_classic_wrappers_are_launchers(entry):
    with pytest.raises(ValidationError, match="запускает другую команду"):
        _with_readonly([entry])


@pytest.mark.parametrize(
    "entry",
    [
        "uv run",
        "npm exec",
        "pnpm dlx",
        "poetry run",
        "docker exec",
        "yarn dlx",
    ],
)
def test_pair_whose_subcommand_runs_any_code_is_rejected(entry):
    """Подкомандой `run`/`exec`/`dlx`/`x` исполняется произвольный код."""
    with pytest.raises(ValidationError, match="произвольный код"):
        _with_readonly([entry])


def test_names_that_merely_end_in_digits_stay_allowed():
    cfg = _with_readonly(["tree", "md5sum"])
    assert cfg.security.permissions.readonly_commands == ["tree", "md5sum"]


@pytest.mark.parametrize("entry", ["git -c", "git -C"])
def test_pair_with_a_flag_as_the_second_word_is_rejected(entry):
    """`git -c` — флаг, а не подкоманда: пара совпала бы с любой командой
    git, начинающейся с этого флага (`git -c core.pager=… log`)."""
    with pytest.raises(ValidationError, match="второе слово флаг"):
        _with_readonly([entry])


def test_entry_of_three_or_more_words_is_rejected():
    """Признаются слово и пара «команда подкоманда»; длиннее — не запись."""
    with pytest.raises(ValidationError, match="команда подкоманда"):
        _with_readonly(["git log --oneline"])


def test_interpreter_list_is_the_reference_list_and_python_variants():
    """Имена эталона и четыре варианта Python — в том виде, в каком их
    сравнивают (нижний регистр, без `.exe`): `cmd.exe` эталона — это `cmd`."""
    assert {"python", "bash", "cmd", "rscript", "env"} <= INTERPRETER_COMMANDS
    assert {"pythonw", "pypy", "ipython", "py"} <= INTERPRETER_COMMANDS
    assert len(INTERPRETER_COMMANDS) == 25
    for names in (INTERPRETER_COMMANDS, COMMAND_LAUNCHERS, RUNNER_TOOLS):
        assert all(name == name.lower() for name in names)


@pytest.mark.parametrize(
    "entry",
    ["uv", "npm", "pnpm", "yarn", "docker", "cargo", "poetry", "NPM"],
)
def test_tool_with_a_code_running_subcommand_needs_a_subcommand(entry):
    """Одним словом `uv` — это и `uv run …`: запись шире пары, которую
    проверка отвергает, поэтому нужна подкоманда."""
    with pytest.raises(ValidationError, match="команда с подкомандами"):
        _with_readonly([entry])


def test_harmless_pairs_of_such_tools_stay_allowed():
    cfg = _with_readonly(["npm ls", "cargo tree", "docker ps"])
    assert cfg.security.permissions.readonly_commands == [
        "npm ls",
        "cargo tree",
        "docker ps",
    ]


@pytest.mark.parametrize("entry", ["python3-config", "php8.2-fpm"])
def test_names_that_only_start_like_an_interpreter_stay_allowed(entry):
    """Срезается только хвост версии и варианта сборки (`-dbg`), а не любое
    `-слово`: `python3-config` и `php8.2-fpm` — не интерпретаторы."""
    cfg = _with_readonly([entry])
    assert cfg.security.permissions.readonly_commands == [entry]
