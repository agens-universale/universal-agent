"""Картинки не попадают в логи запросов (спека §5): data-URL в событии
``backend_request`` маскируется до ``data:<mime>;base64,<N bytes>``, а в сам
запрос уходит полностью."""

from pathlib import Path

from gc_core.utils import connect_backend
from gc_core.utils.logging_module import create_run_logger
from transport_fakes import (
    install_transport,
    json_response,
    sse_body,
    stream_response,
)
from gc_testkit import run_coro
from gc_testkit.settings import make_settings

BLOB = "A" * 4000
DATA_URL = "data:image/png;base64," + BLOB
MASKED = "data:image/png;base64,<4000 bytes>"


def _jsonl_logger(tmp_path):
    cfg = {
        "logs_dump_dir": str(tmp_path),
        "save_logs": True,
        "console_trace": False,
        "logging_format": "jsonl",
        "log_level": "debug",
    }
    logger, path, _ = create_run_logger(
        make_settings(cfg), run_id="mask", agent_kind="main"
    )
    return logger, path


def _log_text(logger, path) -> str:
    for handler in logger.handlers:
        handler.flush()
    return Path(path).read_text(encoding="utf-8")


def test_chat_completions_request_log_masks_image_data(tmp_path, monkeypatch):
    logger, path = _jsonl_logger(tmp_path)
    spy = install_transport(
        monkeypatch,
        json_response({"choices": [{"message": {"content": "ok"}}]}),
    )
    payload = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": DATA_URL}},
                    {"type": "text", "text": "что тут?"},
                ],
            }
        ],
    }

    run_coro(
        connect_backend.arequest_backend_chat_completions(
            payload,
            {},
            url="http://x",
            verify=True,
            logger=logger,
        )
    )

    text = _log_text(logger, path)
    assert BLOB not in text
    assert MASKED in text
    assert BLOB in spy.last_request.content.decode("utf-8")
    assert payload["messages"][0]["content"][0]["image_url"]["url"] == DATA_URL


def test_responses_request_log_masks_image_data(tmp_path, monkeypatch):
    logger, path = _jsonl_logger(tmp_path)
    install_transport(
        monkeypatch,
        stream_response(
            sse_body(
                {
                    "type": "response.completed",
                    "response": {"id": "r1", "output": []},
                },
                done=False,
            )
        ),
    )
    data = {"model": "m", "input": [{"role": "user", "content": DATA_URL}]}

    run_coro(
        connect_backend.arequest_backend_responses(
            data,
            {},
            url="http://x",
            verify=True,
            logger=logger,
        )
    )

    text = _log_text(logger, path)
    assert BLOB not in text
    assert MASKED in text
