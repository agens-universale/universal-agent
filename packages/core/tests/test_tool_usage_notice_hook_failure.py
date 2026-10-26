"""Сбой хука tool_usage_notice перед запросом к модели — warning, ход идёт дальше.

Регресс (ревью PR #135): хук был асинхронным под синхронным именем, и
``messages`` получал объект корутины ДО ``await``. Исключение внутри хука
уходило в ``except Exception``, тот возвращал корутину вместо списка — и
следующий шаг падал на санитайзе (``TypeError``). Хук снова синхронный, как на
develop; тест держит сам сценарий «хук упал».
"""

import inspect
import logging
from types import SimpleNamespace

from gc_core import processing_responses as pr


def _loop(logger):
    loop = object.__new__(pr._AgentLoop)
    loop.session_meta = {}
    loop.state = SimpleNamespace(messages=[])
    loop.ctx = SimpleNamespace()
    loop.run_logger = logger
    loop.persist_messages_callback = None
    return loop


async def test_hook_failure_keeps_original_messages(monkeypatch, caplog):
    def broken(*_args, **_kwargs):
        raise RuntimeError("notice hook broke")

    monkeypatch.setattr(pr, "apply_tool_usage_notice_before_llm", broken)
    messages = [{"type": "message", "role": "user", "content": "hi"}]
    logger = logging.getLogger("test.notice_hook")

    with caplog.at_level(logging.WARNING, logger="test.notice_hook"):
        result = await _loop(logger)._aapply_tool_usage_notice(messages)

    assert not inspect.iscoroutine(result)
    assert result == messages
    assert "Memory messages hook warning" in caplog.text


async def test_hook_success_appends_notice(monkeypatch):
    notice = {"type": "message", "role": "user", "content": "notice"}

    def hook(messages, *_args, **_kwargs):
        return [*messages, notice]

    monkeypatch.setattr(pr, "apply_tool_usage_notice_before_llm", hook)
    messages = [{"type": "message", "role": "user", "content": "hi"}]
    loop = _loop(logging.getLogger("test.notice_hook"))

    result = await loop._aapply_tool_usage_notice(messages)

    assert result == [*messages, notice]
    assert loop.state.messages == [notice]
