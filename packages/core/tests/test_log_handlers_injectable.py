"""Гейт: встраивающий может подключить свою доставку логов.

Абстракции для логирования у ядра нет и не нужно: ``create_run_logger``
возвращает обычный ``logging.Logger``, а лучшего контракта, чем стандартная
библиотека, не придумать. Нужен был только способ подключиться — и мешали ему
ровно две строки:

* ``logger.propagate = False`` — записи не всплывали в корневой логгер, и
  приложение со своей настройкой ``logging`` не видело НИЧЕГО;
* ``logger.handlers.clear()`` — стирало всё, подключённое заранее, поэтому
  прицепиться можно было только ПОСЛЕ сборки контекста, и это нигде не было
  написано.

Теперь: хендлеры принимаются параметром и ставятся после очистки, а
``propagate`` управляется ключом конфига (дефолт прежний).
"""
import logging

import pytest

from gc_core.utils.logging_module import create_run_logger


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())


_CFG = {"save_logs": False, "console_trace": False, "logs_dump_dir": "/tmp"}


def test_custom_handler_receives_records():
    """Свой хендлер получает записи рана."""
    cap = _Capture()
    logger, _path, _trace = create_run_logger(
        dict(_CFG), run_id="t1", agent_kind="main", log_handlers=[cap]
    )
    logger.info("привет")
    assert cap.records == ["привет"]


def test_custom_handler_survives_the_internal_clear():
    """Хендлер ставится ПОСЛЕ ``handlers.clear()`` — иначе бы его стёрло.

    Это и была причина, по которой прицепиться можно было только после сборки
    контекста: ядро чистит хендлеры у именованного логгера в начале сборки.
    """
    cap = _Capture()
    logger, *_ = create_run_logger(
        dict(_CFG), run_id="t2", agent_kind="main", log_handlers=[cap]
    )
    assert cap in logger.handlers


def test_core_handlers_are_not_replaced():
    """Свой хендлер ДОБАВЛЯЕТСЯ, а не вытесняет файловый вывод ядра."""
    cap = _Capture()
    with_files = dict(_CFG)
    with_files["save_logs"] = False        # файла нет, но stderr-хендлер остаётся
    logger, *_ = create_run_logger(
        with_files, run_id="t3", agent_kind="main", log_handlers=[cap]
    )
    assert len(logger.handlers) >= 2, (
        f"хендлеры ядра пропали, остались только {logger.handlers}"
    )


def test_propagate_is_off_by_default():
    """Дефолт прежний: ран не засоряет чужие хендлеры."""
    logger, *_ = create_run_logger(dict(_CFG), run_id="t4", agent_kind="main")
    assert logger.propagate is False


def test_propagate_can_be_enabled():
    """С флагом записи всплывают в корневой логгер приложения."""
    cfg = dict(_CFG)
    cfg["logging_propagate"] = True
    logger, *_ = create_run_logger(cfg, run_id="t5", agent_kind="main")
    assert logger.propagate is True


def test_flag_reaches_flat_config():
    """Ключ доезжает от схемы до плоского словаря, которым живёт рантайм."""
    from gc_core.config_modules.logging_config import LoggingConfig

    assert LoggingConfig().propagate is False
    assert LoggingConfig(propagate=True).propagate is True


def test_context_accepts_handlers():
    """Контракт: параметр есть и у сборки контекста."""
    import inspect

    from gc_core.run_context import create_run_context

    assert "log_handlers" in inspect.signature(create_run_context).parameters


@pytest.mark.asyncio
async def test_handlers_reach_the_context_logger():
    """Сквозная проверка: хендлер, переданный в контекст, ловит записи рана."""
    from gc_core.run_context import acreate_run_context

    cap = _Capture()
    ctx = await acreate_run_context(
        {"stop_tools": [], "backend_api_mode": "chat_completions", "save_logs": False},
        log_handlers=[cap],
    )
    ctx.logger.info("из контекста")
    assert "из контекста" in cap.records
