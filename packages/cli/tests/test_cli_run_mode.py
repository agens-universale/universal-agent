"""gc-cli объявляет свой режим запуска — от него зависит запись журнала правок.

Журнал правок в `gc-cli` (с ним — стенд и фоновые прогоны) по умолчанию не
пишется: там его никто не смотрит. Ядро узнаёт CLI по служебному `run_mode`,
который ставит сам `gc-cli` — после загрузки конфига и CLI-оверрайдов."""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# `import cli` парсит sys.argv при импорте (--query обязателен).
sys.argv = ["gc-cli", "-q", "x"]


def test_cli_declares_the_cli_run_mode():
    from gc_cli import cli as cli_module

    fake_config_dict = {"console_trace": False}
    captured = {}

    async def _capture(config_dict, *args, **kwargs):
        captured["config"] = config_dict
        return SimpleNamespace(logger=MagicMock())

    with (
        patch.object(cli_module, "_ensure_project_gc_dir"),
        patch.object(cli_module, "_ensure_user_skills_dir"),
        patch.object(
            cli_module.arg_parser,
            "run_args",
            SimpleNamespace(
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
                query="hi",
                wrap_technical=False,
                skill=None,
                verbose=False,
                thread_id=None,
                new=False,
                auto_mcp=False,
            ),
        ),
        patch.object(cli_module, "_bootstrap_user_config", return_value=False),
        patch.object(
            cli_module, "load_config_ex", return_value=(fake_config_dict, None)
        ),
        patch.object(cli_module, "apply_cli_overrides"),
        patch.object(
            cli_module,
            "onboarding",
            SimpleNamespace(
                is_unconfigured=lambda cfg: False,
            ),
        ),
        patch.object(cli_module, "_resolve_forced_skill", return_value=None),
        patch.object(cli_module, "build_auth_connector", return_value=None),
        patch.object(
            cli_module,
            "acreate_run_context",
            new=AsyncMock(side_effect=_capture),
        ),
        patch.object(cli_module, "AgentExecutor") as mock_executor_cls,
    ):

        async def _arun(*_a, **_kw):
            return "ok"

        mock_executor_cls.return_value.arun = _arun
        cli_module.main()

    assert captured["config"]["run_mode"] == "cli"


async def test_cli_init_declares_the_cli_run_mode_too():
    """`gc-cli --init` собирает свой контекст отдельно — и тоже объявляет режим.

    Правило одно для всех контекстов gc-cli: без режима ядро приняло бы запуск за
    библиотечный, и `agent_edits.record.cli` на нём бы не действовал."""
    from gc_cli import cli as cli_module

    captured = {}

    async def _capture(config_dict, *args, **kwargs):
        captured["config"] = config_dict
        return SimpleNamespace(logger=MagicMock())

    executor = MagicMock()
    executor.return_value.arun = AsyncMock(return_value="ok")
    with (
        patch.object(cli_module, "load_config", return_value={}),
        patch.object(cli_module, "build_auth_connector", return_value=None),
        patch.object(
            cli_module,
            "acreate_run_context",
            new=AsyncMock(side_effect=_capture),
        ),
        patch.object(cli_module, "AgentExecutor", executor),
        patch(
            "gc_core.security.confirm_port.confirm_available",
            return_value=False,
        ),
        patch("gc_core.utils.async_http.aclose_clients", new=AsyncMock()),
    ):
        await cli_module._run_init_agents_md()

    assert captured["config"]["run_mode"] == "cli"
