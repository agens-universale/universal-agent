from gc_core import manual_compaction
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def test_compact_thread_noop_in_responses_mode():
    ctx = fake_ctx(
        {"backend.api_mode": "responses"},
        tools_spec=[],
        tools_description="",
        logger=None,
    )
    assert run_coro(manual_compaction.acompact_thread(ctx, "t-1")) is False


def test_compact_thread_noop_on_empty_thread_id():
    ctx = fake_ctx(
        {"backend.api_mode": "chat_completions"},
        tools_spec=[],
        tools_description="",
        logger=None,
    )
    assert run_coro(manual_compaction.acompact_thread(ctx, "")) is False
