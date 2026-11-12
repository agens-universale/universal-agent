"""`gc-cli --init` (`_run_init_agents_md`) собирает RunContext тем же
`force_legacy`, что и основной путь запуска (`cli.main()`), не молчаливым
дефолтом `False`."""

import sys
from types import SimpleNamespace

# `import cli` парсит sys.argv на импорте — пин валидного argv (тот же приём,
# что в test_cli_broken_config_wiring.py).
sys.argv = ["gc-cli", "-q", "x"]

from gc_cli import cli
from gc_testkit import run_coro
from gc_testkit.settings import make_settings


async def _init_prompt(**_kw):
    return "prompt"


def test_init_agents_md_threads_force_legacy_into_run_context(monkeypatch):
    captured = {}

    async def _fake_create_run_context(
        loaded, *, auth_connector=None, force_legacy=False, **kw
    ):
        captured["force_legacy"] = force_legacy
        # Не голый object(): `--init` вешает на контекст порт `ask_human`
        # (поле RunContext), двойнику нужны атрибуты.
        return SimpleNamespace()

    class _FakeExecutor:
        def __init__(self, ctx):
            pass

        async def arun(self, messages):
            return None

    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda path, **kw: make_settings({"models": {"m": {}}}).with_overrides(
            {"agent.model": "forced-model"}, layer="cli"
        ),
    )
    monkeypatch.setattr(cli, "build_auth_connector", lambda cfg: None)
    monkeypatch.setattr(cli, "acreate_run_context", _fake_create_run_context)
    monkeypatch.setattr(cli, "AgentExecutor", _FakeExecutor)
    monkeypatch.setattr(
        "gc_core.instructions.init_agents_md.abuild_init_prompt",
        _init_prompt,
    )
    monkeypatch.setattr(
        "gc_core.security.confirm_port.confirm_available", lambda ctx: True
    )
    monkeypatch.setattr(cli.arg_parser.run_args, "config", None, raising=False)

    rc = run_coro(cli._run_init_agents_md())

    assert rc == 0
    # models непуст + agent.model задан слоем cli — легаси-форс обязан
    # дойти до RunContext, а не остаться дефолтным False.
    assert captured["force_legacy"] is True
