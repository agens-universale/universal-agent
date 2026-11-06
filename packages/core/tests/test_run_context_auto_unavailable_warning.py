"""`level: auto` при недоступном автовыборе (спека auto-routing §6): сборка
контекста пишет текст уведомления warning'ом в лог прогона. Точка одна на все
входы — TUI, CLI, сервер и `AsyncAgent`; показать уведомление пользователю
своим UI — дело фронтенда.

Логгер прогона по умолчанию не всплывает в корневой (`propagate=False`), поэтому
записи ловит свой хендлер, переданный в сборку через `log_handlers`.
"""

import logging

import pytest

from gc_core import run_context as rc
from gc_core.startup_notices import auto_routing_unavailable_notice
from gc_testkit import as_coro
from gc_testkit.settings import make_settings


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.levelno, record.getMessage()))


@pytest.fixture(autouse=True)
def _no_discovery(monkeypatch):
    monkeypatch.setattr(
        rc, "abuild_tools_data", as_coro(lambda cfg, **_kw: ({}, [], [], {}))
    )


def _cfg(tmp_path, **over) -> dict:
    cfg = {
        "model": "auto",
        "auto_routing": True,
        "backend_api_mode": "chat_completions",
        "models": {"fast": {}, "mid": {}, "big": {}},
        "slots": {"light": "fast", "default": "mid", "max": "big"},
        "save_logs": False,
        "console_trace": False,
        "logs_dump_dir": str(tmp_path),
    }
    cfg.update(over)
    return make_settings(
        {
            **{
                k: v
                for k, v in cfg.items()
                if k in ("model", "models", "slots")
            },
            "agent.auto_routing": cfg["auto_routing"],
            "backend.api_mode": cfg["backend_api_mode"],
            "logging.save_logs": cfg["save_logs"],
            "logging.console_trace": cfg["console_trace"],
            "paths.logs_dump_dir": cfg["logs_dump_dir"],
        }
    )


async def _run_log_warnings(cfg: dict) -> list:
    cap = _Capture()
    await rc.acreate_run_context(cfg, log_handlers=[cap])
    return [
        message for level, message in cap.records if level == logging.WARNING
    ]


@pytest.mark.parametrize(
    "over",
    [
        {"backend_api_mode": "responses"},
        {"auto_routing": False},
        {"models": {"only": {}}, "slots": {"default": "only"}},
    ],
)
async def test_unavailable_auto_is_a_warning_in_the_run_log(tmp_path, over):
    cfg = _cfg(tmp_path, **over)
    assert auto_routing_unavailable_notice(
        cfg
    ).message in await _run_log_warnings(cfg)


@pytest.mark.parametrize(
    "over",
    [
        {},  # auto доступен
        {
            "model": "default",
            "backend_api_mode": "responses",
        },  # auto не выбран
    ],
)
async def test_no_warning_when_auto_is_available_or_not_selected(
    tmp_path, over
):
    cfg = _cfg(tmp_path, **over)
    assert auto_routing_unavailable_notice(cfg) is None
    warnings = await _run_log_warnings(cfg)
    assert [m for m in warnings if "автовыбор недоступен" in m] == []
