"""Финальная точка учёта видит токены, списанные уже после цикла."""

from types import SimpleNamespace

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.usage.account import (
    CATEGORY_MAIN,
    CATEGORY_STRUCTURED_OUTPUT,
    UsageAccount,
)


class _Logger:
    def __init__(self):
        self.events = []

    def info(self, msg, *args, extra=None):
        if extra:
            self.events.append(extra)

    def warning(self, msg, *args, **kwargs):
        return None


class _Session:
    def __init__(self, meta):
        self.meta = meta
        self.backend_api_mode = "chat_completions"
        self.batches = []

    async def apersist_batch(self, messages, *, previous_response_id):
        self.batches.append((list(messages), previous_response_id))


def _state(*, usage, start, last_run, stop=False):
    return SimpleNamespace(
        usage=usage,
        run_start_usage_meta=start.to_meta(),
        last_run_usage=last_run,
        user_intent_log=[],
        current_user_intent=[],
        user_turn_seq=0,
        dialog_steps_offset=0,
        last_main_prompt_tokens=None,
        last_main_prompt_message_count=None,
        shared=None,
        previous_response_id="resp-1",
        backend_api_mode="chat_completions",
        discard_writes_after_stop=stop,
        stop_requested=stop,
    )


def _executor(state, session):
    logger = _Logger()
    ctx = SimpleNamespace(
        config_dict={
            "console_trace": True,
            "logging_format": "text",
            "model": "m",
            "completions_url": "http://llm",
        },
        logger=logger,
    )
    ex = AgentExecutor(ctx)
    ex._state = state
    ex._thread_session = session
    return ex, logger


@pytest.mark.asyncio
async def test_close_includes_tokens_added_after_the_loop(capsys):
    """Шаги после цикла успевают списать токены до закрытия учёта."""
    start = UsageAccount()
    usage = UsageAccount()
    usage.add(CATEGORY_MAIN, 100, 10)
    stale = UsageAccount.from_meta(usage.to_meta())
    usage.add(CATEGORY_STRUCTURED_OUTPUT, 7, 4)
    state = _state(usage=usage, start=start, last_run=stale)
    session = _Session(meta={"token_usage": stale.to_meta()})
    ex, logger = _executor(state, session)

    await ex.aclose_run_accounting(console=False)

    run = state.last_run_usage
    assert run.by_category[CATEGORY_MAIN]["input"] == 100
    assert run.by_category[CATEGORY_STRUCTURED_OUTPUT]["input"] == 7
    assert run.by_category[CATEGORY_STRUCTURED_OUTPUT]["output"] == 4
    stored = session.meta["token_usage"]["by_category"]
    assert stored[CATEGORY_STRUCTURED_OUTPUT]["input"] == 7
    assert session.batches == [([], "resp-1")]
    events = [
        extra for extra in logger.events if extra.get("event") == "run_usage"
    ]
    assert len(events) == 1
    categories = events[0]["usage"]["categories"]
    assert categories[CATEGORY_STRUCTURED_OUTPUT]["input"] == 7
    out, _err = capsys.readouterr()
    assert out == ""


@pytest.mark.asyncio
async def test_second_close_does_not_write_again():
    usage = UsageAccount()
    usage.add(CATEGORY_MAIN, 10, 1)
    state = _state(usage=usage, start=UsageAccount(), last_run=None)
    session = _Session(meta={})
    ex, logger = _executor(state, session)

    await ex.aclose_run_accounting(console=False)
    usage.add(CATEGORY_STRUCTURED_OUTPUT, 5, 1)
    await ex.aclose_run_accounting(console=False)

    assert len(session.batches) == 1
    stored = session.meta["token_usage"]["by_category"]
    assert CATEGORY_STRUCTURED_OUTPUT not in stored
    events = [
        extra for extra in logger.events if extra.get("event") == "run_usage"
    ]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_close_skips_persist_when_stop_discarded_writes():
    usage = UsageAccount()
    usage.add(CATEGORY_STRUCTURED_OUTPUT, 3, 1)
    state = _state(
        usage=usage,
        start=UsageAccount(),
        last_run=UsageAccount(),
        stop=True,
    )
    session = _Session(meta={})
    ex, _logger = _executor(state, session)

    await ex.aclose_run_accounting(console=False)

    structured = state.last_run_usage.by_category[CATEGORY_STRUCTURED_OUTPUT]
    assert structured["input"] == 3
    assert session.batches == []
    assert "token_usage" not in session.meta
