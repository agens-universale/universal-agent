"""Входы режима `auto` в gc-cli: недоступный автовыбор называет причину тем же
текстом, что и остальные входы, агент работает уровнем default, код возврата не
меняется. Настоящий `cli.main()` на настоящем конфиге с диска; подменены только
сборка RunContext, исполнитель и штамп обновления. Managed- и проектный слои
конфига изолирует тест-кит (`_isolated_config_layers`)."""

import importlib
import logging
import sys
from types import SimpleNamespace

import pytest

# `import cli` парсит sys.argv на импорте — пин валидного argv (тот же приём,
# что в test_cli_broken_config_wiring.py).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import cli
from gc_core.auto_routing.levels import auto_available
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import make_settings

_BACKEND = (
    "backend:\n  api_mode: chat_completions\n"
    '  completions_url: "http://127.0.0.1:8014/v1/chat/completions"\n'
)
_ONE_MODEL = "models:\n  only: {}\nslots:\n  default: only\n"
_THREE_MODELS = (
    "models:\n  fast: {}\n  mid: {}\n  big: {}\n"
    "slots:\n  light: fast\n  default: mid\n  max: big\n"
)


@pytest.fixture(autouse=True)
def _restore_arg_parser():
    """Вернуть модулю разбора аргументов пин-argv файла: перезагрузка с чужим
    argv иначе переживает тест и достаётся соседям по процессу."""
    yield
    saved, sys.argv = sys.argv, ["gc-cli", "-q", "x"]
    try:
        from gc_cli import arg_parser as ap_mod

        importlib.reload(ap_mod)
    finally:
        sys.argv = saved


def _reload_arg_parser_with_argv(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", argv)
    from gc_cli import arg_parser as ap_mod

    importlib.reload(ap_mod)
    return ap_mod


class _Executor:
    def __init__(self, ctx):
        pass

    async def arun(self, *args, **kwargs):
        return None


@pytest.mark.parametrize(
    "body, flags, reason, tail",
    [
        (
            _BACKEND + _ONE_MODEL,
            ["--level", "auto"],
            "во всех уровнях одна модель",
            "уровень default",
        ),
        (
            _BACKEND + _THREE_MODELS + "level: auto\n",
            ["--agent.model", "other"],
            "уровни отключены флагом --model",
            "модель из --model",
        ),
    ],
    ids=["level_flag_on_one_model", "model_flag"],
)
def test_main_names_the_reason_runs_default_and_exits_0(
    monkeypatch, tmp_path, capsys, body, flags, reason, tail
):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "base.yml"
    base.write_text(body, encoding="utf-8")
    _reload_arg_parser_with_argv(
        ["gc-cli", "--config", str(base), "-q", "x", *flags], monkeypatch
    )
    seen = {}

    async def _fake_create_run_context(
        loaded, *, auth_connector=None, force_legacy=False, **kw
    ):
        seen["available"] = auto_available(loaded, force_legacy=force_legacy)
        return SimpleNamespace(
            mcp_status=None,
            logger=logging.getLogger("test_cli_auto_routing_entries"),
        )

    monkeypatch.setattr(cli, "_ensure_project_gc_dir", lambda: None)
    monkeypatch.setattr(cli, "_ensure_user_skills_dir", lambda: None)
    monkeypatch.setattr(cli, "build_auth_connector", lambda cfg: None)
    monkeypatch.setattr(cli, "acreate_run_context", _fake_create_run_context)
    monkeypatch.setattr(cli, "AgentExecutor", _Executor)
    monkeypatch.setattr("gc_core.upgrade_gate.detect_upgrade", lambda: None)
    monkeypatch.setattr("gc_core.upgrade_gate.mark_current_run", lambda: None)

    rc = cli.main()

    assert rc == 0
    assert seen["available"] is False  # RunContext соберётся на default
    assert (
        f"автовыбор моделей недоступен: {reason} — работает {tail}"
        in capsys.readouterr().err
    )


def test_init_agents_md_prints_the_same_startup_notices(monkeypatch, capsys):
    """`gc-cli --init` запускает того же агента — и те же стартовые уведомления,
    что основной путь: `level: auto` на одной модели называет причину."""
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path, **kw: make_settings(
            {
                "level": "auto",
                "backend.api_mode": "chat_completions",
                "models": {"only": {}},
                "slots": {"default": "only"},
            }
        ),
    )
    monkeypatch.setattr(cli, "build_auth_connector", lambda cfg: None)
    monkeypatch.setattr(
        cli,
        "acreate_run_context",
        as_coro(
            lambda loaded, *, auth_connector=None, force_legacy=False, **kw: (
                SimpleNamespace(mcp_status=None)
            )
        ),
    )
    monkeypatch.setattr(cli, "AgentExecutor", _Executor)
    monkeypatch.setattr(
        "gc_core.instructions.init_agents_md.build_init_prompt",
        lambda **kw: "prompt",
    )
    monkeypatch.setattr(
        "gc_core.security.confirm_port.confirm_available", lambda ctx: True
    )
    monkeypatch.setattr(cli.arg_parser.run_args, "config", None, raising=False)

    rc = run_coro(cli._run_init_agents_md())

    assert rc == 0
    assert (
        "автовыбор моделей недоступен: во всех уровнях одна модель — работает уровень default"
        in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "profile_level, flags",
    [
        ("max", ["--level", "auto"]),
        ("auto", []),
    ],
    ids=["explicit_auto_over_profile_level", "auto_from_profile"],
)
def test_init_flags_reach_typed_settings_and_actual_router(
    monkeypatch, tmp_path, profile_level, flags
):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "base.yml"
    base.write_text(
        _BACKEND
        + _THREE_MODELS
        + "level: default\nlogging:\n  save_logs: false\n",
        encoding="utf-8",
    )
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    (profiles / "review.yml").write_text(
        f"level: {profile_level}\nmodels:\n  mid:\n    model: profile-mid\n",
        encoding="utf-8",
    )
    _reload_arg_parser_with_argv(
        [
            "gc-cli",
            "--init",
            "--config",
            str(base),
            "--profile",
            "review",
            *flags,
        ],
        monkeypatch,
    )
    seen = {}

    class _InitExecutor(_Executor):
        def __init__(self, ctx):
            seen["ctx"] = ctx

    monkeypatch.setattr(cli, "build_auth_connector", lambda loaded: None)
    monkeypatch.setattr(cli, "AgentExecutor", _InitExecutor)
    monkeypatch.setattr(
        "gc_core.instructions.init_agents_md.build_init_prompt",
        lambda **kw: "prompt",
    )

    assert run_coro(cli._run_init_agents_md()) == 0

    ctx = seen["ctx"]
    assert ctx.settings.level == "auto"
    assert ctx.runtime.active_profile == "review"
    assert ctx.runtime.run_mode == "cli"
    assert ctx.auto_available is True
    assert ctx.model_router.live_choice() == "auto"
    assert ctx.model_router.resolve("main").model == "profile-mid"


@pytest.mark.parametrize(
    "flags, exit_code, reason",
    [
        (["--profile", "missing"], 1, "missing"),
        (["--agent.auto_routing_two_level.weak_steps", "0"], 2, "weak_steps"),
    ],
)
def test_init_rejects_invalid_settings_before_context_creation(
    monkeypatch, tmp_path, capsys, flags, exit_code, reason
):
    monkeypatch.chdir(tmp_path)
    base = tmp_path / "base.yml"
    base.write_text(_BACKEND + _THREE_MODELS, encoding="utf-8")
    _reload_arg_parser_with_argv(
        [
            "gc-cli",
            "--init",
            "--config",
            str(base),
            *flags,
        ],
        monkeypatch,
    )

    async def _unexpected_context(*args, **kwargs):
        pytest.fail("invalid settings must stop before context creation")

    monkeypatch.setattr(cli, "acreate_run_context", _unexpected_context)

    assert run_coro(cli._run_init_agents_md()) == exit_code
    err = capsys.readouterr().err
    assert reason in err
    assert "Traceback" not in err
