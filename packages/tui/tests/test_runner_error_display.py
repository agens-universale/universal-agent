"""Ошибка хода в TUI: понятный текст на экране и трейсбек в логе прогона.

Сырой ``JSONDecodeError`` показывался как «runtime: Expecting value: line 1
column 1 (char 0)» — ни типа, ни того, что сломался ответ сервера модели. А в
лог прогона исключение хода не попадало вовсе, и искать источник было не по
чему.
"""

import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core.utils.backend_errors import BackendBadResponseError
from gc_testkit.tui_support import tui_settings
from gc_tui.services.agent_runner import AgentRunner, run_error_text


def _bad_response(api_mode="chat_completions") -> BackendBadResponseError:
    return BackendBadResponseError(
        status_code=200,
        content_type="text/plain",
        length=0,
        api_mode=api_mode,
    )


def test_bad_response_text_is_human():
    assert run_error_text(_bad_response()) == (
        "Сервер модели вернул пустой или не-JSON ответ (HTTP 200, 0 байт) — "
        "возможно, прокси не поддерживает stream:false; повторите запрос"
    )


def test_bad_response_hint_in_responses_mode_is_about_the_stream():
    """В режиме responses запрос всегда стримовый: совет про stream:false
    там ложный — подсказка о потоке, который прокси оборвал или испортил."""
    text = run_error_text(_bad_response("responses"))

    assert "stream:false" not in text
    assert text == (
        "Сервер модели вернул пустой или не-JSON ответ (HTTP 200, 0 байт) — "
        "возможно, прокси оборвал или испортил поток ответа; "
        "повторите запрос"
    )


def test_other_error_text_names_exception_type():
    exc = json.JSONDecodeError("Expecting value", "", 0)

    assert run_error_text(exc) == (
        "runtime: JSONDecodeError: Expecting value: line 1 column 1 (char 0)"
    )


def test_error_without_message_shows_type_only():
    assert run_error_text(RuntimeError()) == "runtime: RuntimeError"


class _Records(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


class _Boom:
    def __init__(self, exc, logger=None):
        self._exc = exc
        if logger is not None:
            self._ctx = SimpleNamespace(logger=logger)

    async def arun(self, messages, *, thread_id, **kw):
        raise self._exc

    def stop_run(self):
        pass

    def add_message(self, m):
        pass


def _run_failing(executor) -> None:
    called = threading.Event()
    runner = AgentRunner(executor=executor, on_error=lambda e: called.set())
    assert runner.start(query="x", thread_id="t1") is True
    assert called.wait(timeout=2.0), "on_error не вызван"
    runner.wait(timeout=2.0)


def _raise_from_helper():
    raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_run_error_is_logged_to_run_logger_with_traceback():
    logger = logging.getLogger("test_runner_error_display.run")
    handler = _Records()
    logger.addHandler(handler)
    try:
        try:
            _raise_from_helper()
        except ValueError as raised:
            exc = raised
        _run_failing(_Boom(exc, logger=logger))
    finally:
        logger.removeHandler(handler)

    errors = [r for r in handler.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1
    record = errors[0]
    assert record.exc_info is not None and record.exc_info[1] is exc
    text = logging.Formatter().format(record)
    assert "Traceback" in text
    assert "_raise_from_helper" in text
    assert "ValueError" in text


def test_run_error_without_run_logger_goes_to_module_log(caplog):
    with caplog.at_level(logging.ERROR, logger="gc_tui.services.agent_runner"):
        _run_failing(_Boom(RuntimeError("boom")))

    errors = [
        r
        for r in caplog.records
        if r.name == "gc_tui.services.agent_runner"
        and r.levelno >= logging.ERROR
    ]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


async def _trace_after_failed_turn(tmp_path, exc) -> str:
    from gc_tui.app import TuiApp

    class _Fake(_Boom):
        async def arun(self, messages, *, thread_id, **kw):
            (Path(tmp_path) / f"meta_cli_{thread_id}.json").write_text(
                json.dumps({"thread_id": thread_id, "messages_count": 0}),
                encoding="utf-8",
            )
            raise self._exc

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake(exc)
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "привет"
        await pilot.press("enter")
        await pilot.pause(0.5)
        return "\n".join(
            str(s.render()) for s in pilot.app.screen.query("#trace Static")
        )


@pytest.mark.asyncio
async def test_trace_shows_exception_type(tmp_path):
    pytest.importorskip("textual")

    flat = await _trace_after_failed_turn(
        tmp_path, json.JSONDecodeError("Expecting value", "", 0)
    )

    assert "runtime: JSONDecodeError: Expecting value" in flat


@pytest.mark.asyncio
async def test_trace_shows_human_text_for_bad_response(tmp_path):
    pytest.importorskip("textual")

    flat = await _trace_after_failed_turn(tmp_path, _bad_response())

    assert "Сервер модели вернул пустой или не-JSON ответ" in flat
    assert "runtime:" not in flat
