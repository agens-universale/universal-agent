"""State.produced_output — прогон что-то выдал или сделал.

Фронтенд по нему решает, можно ли отменить отправку без следов: до первой
дельты стрима, первой записи агента и первого старта тула — можно.
Служебные записи (реплика, техзаметка режима) флаг не ставят.
"""

from gc_core.processing_responses import (
    _abefore_tool_call,
    _main_call_on_delta,
    _make_technical_feedback_user_message,
    _maybe_persist,
)
from gc_core.states import State


def _state() -> State:
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


class _Ctx:
    def __init__(self):
        self.deltas = []

    def on_stream_delta(self, kind, text):
        self.deltas.append((kind, text))


def test_fresh_state_has_produced_nothing():
    assert _state().produced_output is False


async def test_user_reply_and_mode_notice_do_not_count_as_output():
    state = _state()
    notice = _make_technical_feedback_user_message("режим: plan")

    await _maybe_persist(
        lambda _m: None, [{"role": "user", "content": "x"}, notice], state
    )

    assert state.produced_output is False


async def test_persisted_agent_record_counts_as_output():
    state = _state()

    await _maybe_persist(
        lambda _m: None,
        [{"type": "function_call", "name": "fs_read", "call_id": "c1"}],
        state,
    )

    assert state.produced_output is True


async def test_tool_start_counts_as_output():
    state = _state()

    await _abefore_tool_call(
        0, "fs_edit", state, prior=[], persist_messages_callback=None
    )

    assert state.produced_output is True


def test_reasoning_delta_counts_as_output():
    state, ctx = _state(), _Ctx()

    sink = _main_call_on_delta(ctx, state, stream_on=True, mute_answer=False)
    sink("reset", "")
    assert state.produced_output is False
    sink("think", "думаю")

    assert state.produced_output is True
    assert ctx.deltas == [("reset", ""), ("think", "думаю")]


def test_muted_answer_delta_is_not_output():
    state, ctx = _state(), _Ctx()

    sink = _main_call_on_delta(ctx, state, stream_on=True, mute_answer=True)
    sink("answer", "текст")

    assert state.produced_output is False
    assert ctx.deltas == []


def test_no_stream_no_sink():
    assert (
        _main_call_on_delta(
            _Ctx(), _state(), stream_on=False, mute_answer=False
        )
        is None
    )
