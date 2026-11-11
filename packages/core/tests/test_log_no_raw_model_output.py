"""Сырой вывод модели не попадает в предупреждения и ошибки лога.

Маскировка секретов узнаёт их по имени ключа только в словаре; вывод модели
(аргументы вызова, чанки стрима, весь ответ) в сообщение лога уходит строкой —
там поле ``password`` уже не ключ, и значение утекало бы в stdout, файл и
обработчики приложения. На warning и error — только метаданные, текст — на
debug. Журнал диалога на info пишет сообщения целиком — это его назначение.
"""

import json
import logging

import pytest
from gc_core.utils import connect_backend
from gc_core.utils.convertations import (
    get_output_list,
    output_chat_completion_to_response,
)
from gc_core.utils.logging_module import create_run_logger, release_run_logger
from gc_core.utils.stream_assembler import StreamAssembler
from gc_testkit.settings import make_settings

SECRET = "hunter2-correct-horse"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def run_log(tmp_path, capsys):
    """Логгер прогона как у коллеги: jsonl, warning, маскировка включена.
    Собирается при первом обращении из теста: stdout-обработчик должен
    держать поток, который перехватывает capsys на время самого теста."""
    capture = _Capture()
    built = []

    def logger():
        if not built:
            settings = make_settings(
                {
                    "paths.logs_dump_dir": str(tmp_path),
                    "logging.save_logs": True,
                    "logging.console_trace": True,
                    "logging.format": "jsonl",
                    "logging.log_level": "warning",
                    "logging.redact_secrets.enabled": True,
                }
            )
            built.extend(
                create_run_logger(
                    settings, run_id="leak", log_handlers=[capture]
                )
            )
        return built[0]

    def channels() -> list[str]:
        for handler in built[0].handlers:
            handler.flush()
        with open(built[1], encoding="utf-8") as fh:
            file_text = fh.read()
        return [file_text, capsys.readouterr().out, "\n".join(capture.lines)]

    yield logger, channels
    if built:
        release_run_logger(built[0])


def _choice(finish_reason, arguments):
    return {
        "id": "resp-1",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "fs_file_info",
                                "arguments": arguments,
                            },
                        }
                    ],
                },
            }
        ],
    }


def _assert_warned_without_secret(channels, marker):
    file_text, stdout, handler = channels()
    for text in (file_text, stdout, handler):
        assert marker in text
        assert SECRET not in text


def test_truncated_arguments(run_log):
    make_logger, channels = run_log
    logger = make_logger()
    broken = '{"path": "a.toml", "password": "' + SECRET + '", "x": '
    get_output_list(_choice("tool_calls", broken), logger_override=logger)
    _assert_warned_without_secret(channels, "not valid JSON")


def test_dropped_stream_chunk(run_log):
    make_logger, channels = run_log
    logger = make_logger()
    asm = StreamAssembler()
    asm.feed_line('data: {"password": "' + SECRET + '", "choices": [')
    connect_backend._log_dropped_chunks(logger, asm)
    _assert_warned_without_secret(channels, "dropped")


def test_length_finish(run_log):
    make_logger, channels = run_log
    logger = make_logger()
    args = json.dumps({"path": "a", "password": SECRET})
    get_output_list(_choice("length", args), logger_override=logger)
    _assert_warned_without_secret(channels, "finished with length")


def test_unknown_finish(run_log):
    make_logger, channels = run_log
    logger = make_logger()
    args = json.dumps({"path": "a", "password": SECRET})
    get_output_list(_choice("weird", args), logger_override=logger)
    _assert_warned_without_secret(channels, "accepted permissively")


def test_empty_choice(run_log):
    make_logger, channels = run_log
    logger = make_logger()
    dct = {
        "id": "resp-1",
        "model": "m",
        "note": SECRET,
        "choices": [
            {"index": 0, "finish_reason": None, "message": {"content": ""}}
        ],
    }
    get_output_list(dct, logger_override=logger)
    _assert_warned_without_secret(channels, "Empty choice discarded")


def test_failed_conversion(run_log, monkeypatch):
    make_logger, channels = run_log
    logger = make_logger()
    from gc_core.utils import convertations

    def _boom(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(convertations, "get_output_list", _boom)
    args = json.dumps({"path": "a", "password": SECRET})
    with pytest.raises(ValueError):
        output_chat_completion_to_response(
            _choice("tool_calls", args), logger_override=logger
        )
    _assert_warned_without_secret(channels, "Failed to convert")
