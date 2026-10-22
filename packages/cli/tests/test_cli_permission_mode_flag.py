"""`gc-cli --permission-mode`: флаг разбирается и доезжает до исполнителя.

Флаг — выбор режима на эту сессию: он сильнее режима, сохранённого в треде,
поэтому отдаётся исполнителю, а не кладётся слоем конфига.
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# `import cli` парсит sys.argv при импорте (--query обязателен).
sys.argv = ["gc-cli", "-q", "x"]


def test_flag_accepts_available_modes_only():
    from gc_cli import arg_parser
    from gc_core.security.permission_mode import AVAILABLE_MODES

    for mode in AVAILABLE_MODES:
        parsed, _ = arg_parser.parser.parse_known_args(
            ["-q", "x", "--permission-mode", mode.value]
        )
        assert parsed.permission_mode == mode.value
    parsed, _ = arg_parser.parser.parse_known_args(["-q", "x"])
    assert parsed.permission_mode is None
    parsed, _ = arg_parser.parser.parse_known_args(
        ["-q", "x", "--permission-mode", "Plan"]
    )
    assert parsed.permission_mode == "plan"
    with pytest.raises(SystemExit):
        arg_parser.parser.parse_known_args(
            ["-q", "x", "--permission-mode", "турбо"]
        )


def _run_main(permission_mode):
    """Прогнать `main()` на заглушках; вернуть экземпляр исполнителя."""
    from gc_cli import cli as cli_module

    from gc_testkit.settings import make_settings

    async def _context(settings, *args, **kwargs):
        return SimpleNamespace(logger=MagicMock())

    run_args = SimpleNamespace(
        init_config=False,
        init=False,
        config=None,
        profile=None,
        write_guard=False,
        no_write_guard=False,
        command_moderation=None,
        read_guard=None,
        no_read_guard=False,
        read_guard_mode=None,
        permission_mode=permission_mode,
        query="hi",
        wrap_technical=False,
        skill=None,
        verbose=False,
        thread_id=None,
        new=False,
        auto_mcp=False,
    )
    with (
        patch.object(cli_module, "_ensure_project_gc_dir"),
        patch.object(cli_module, "_ensure_user_skills_dir"),
        patch.object(cli_module.arg_parser, "run_args", run_args),
        patch.object(cli_module, "_bootstrap_user_config", return_value=False),
        patch.object(
            cli_module,
            "load_settings",
            return_value=make_settings({"logging.console_trace": False}),
        ),
        patch.object(
            cli_module,
            "apply_cli_overrides",
            side_effect=lambda loaded, **_kw: loaded,
        ),
        patch.object(
            cli_module,
            "onboarding",
            SimpleNamespace(is_unconfigured=lambda cfg: False),
        ),
        patch.object(cli_module, "_resolve_forced_skill", return_value=None),
        patch.object(cli_module, "build_auth_connector", return_value=None),
        patch.object(
            cli_module,
            "acreate_run_context",
            new=AsyncMock(side_effect=_context),
        ),
        patch.object(cli_module, "AgentExecutor") as executor_cls,
    ):
        executor_cls.return_value.arun = AsyncMock(return_value="ok")
        cli_module.main()
        return executor_cls.return_value


def test_flag_reaches_the_executor_before_the_run():
    executor = _run_main("manual")
    executor.set_permission_mode.assert_called_once_with("manual")


def test_without_the_flag_the_executor_is_left_to_the_config():
    executor = _run_main(None)
    executor.set_permission_mode.assert_not_called()
