"""Сбой сборки подсказок перед запросом к модели — warning, ход идёт дальше.

Регресс (ревью PR #135): исключение внутри хука не должно ронять шаг.
Текст подсказки при этом не попадает в буфер, остальные наджи собираются.
"""

import logging
from types import SimpleNamespace

from gc_core.message_policy.nudges import NudgeBuffer, afill_nudge_buffer
from gc_testkit.settings import fake_ctx


def _state():
    return SimpleNamespace(
        mode_announced=None,
        tick_signals=set(),
        loop_detector_pending=[],
    )


async def test_hook_failure_leaves_the_buffer_without_the_notice(
    monkeypatch, caplog
):
    def broken(*_args, **_kwargs):
        raise RuntimeError("notice hook broke")

    monkeypatch.setattr(
        "gc_core.message_policy.nudges.notice_texts_before_llm", broken
    )
    buffer = NudgeBuffer()
    logger = logging.getLogger("test.notice_hook")

    with caplog.at_level(logging.WARNING, logger="test.notice_hook"):
        await afill_nudge_buffer(
            buffer,
            _state(),
            fake_ctx(),
            session_meta={},
            step_messages=[],
            backend_mode="chat_completions",
            logger=logger,
        )

    assert buffer.texts == []
    assert "Memory messages hook warning" in caplog.text


async def test_hook_success_puts_the_notice_in_the_buffer(monkeypatch):
    def hook(*_args, **_kwargs):
        return ["notice"]

    monkeypatch.setattr(
        "gc_core.message_policy.nudges.notice_texts_before_llm", hook
    )
    buffer = NudgeBuffer()

    await afill_nudge_buffer(
        buffer,
        _state(),
        fake_ctx(),
        session_meta={},
        step_messages=[],
        backend_mode="chat_completions",
        logger=None,
    )

    assert buffer.texts == ["notice"]
