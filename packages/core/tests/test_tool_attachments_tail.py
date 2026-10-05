"""Картинки батча — одним техническим сообщением в самом конце (спека §4, §7.1).

Тул-сообщение chat/completions картинку не несёт, поэтому картинки из тулов и из
ответа на turn_ask_human едут отдельным user-сообщением после батча: после
пересборки session_compact и после напоминания loop detector. После стопа
пользователя его нет, после принятого turn_finish оно уходит вместе с хвостом.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core import processing_responses as pr
from gc_core.attachments.model import (
    ORIGIN_TOOL,
    ORIGIN_USER,
    TOOL_LABEL,
    Attachment,
    user_label,
)
from gc_core.loop_detector.constants import LOOP_WARN_KEY, READ_PROFILE
from gc_core.message_policy.tool_attachments import (
    build_tool_attachments_message,
)
from gc_core.states import State
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx


def _att(**overrides) -> Attachment:
    base = dict(
        id="3f9a1c27b0e4d5a6",
        kind="image",
        mime="image/png",
        width=1456,
        height=819,
        size=214880,
        path="/threads/images/cli_t1_3f9a1c27b0e4d5a6.png",
        origin=ORIGIN_TOOL,
        label=TOOL_LABEL,
        source="/proj/shot.png",
        orig_width=2912,
        orig_height=1638,
    )
    base.update(overrides)
    return Attachment(**base)


TOOL_ATT = _att()
ASK_ATT = _att(
    id="0b1c2d3e4f5a6b7c",
    mime="image/jpeg",
    width=800,
    height=600,
    path="/threads/images/cli_t1_0b1c2d3e4f5a6b7c.jpg",
    origin=ORIGIN_USER,
    label=user_label(3),
    source="clipboard",
    orig_width=800,
    orig_height=600,
)


# --- сборка сообщения ------------------------------------------------------


def test_one_line_per_image_by_origin():
    msg = build_tool_attachments_message(
        [TOOL_ATT.to_dict(), ASK_ATT.to_dict()], message_id="m_x"
    )

    assert msg == {
        "role": "user",
        "content": (
            '<technical type="technical">'
            "Image from fs_view_image: /proj/shot.png · png 1456×819\n"
            "Изображение #3 from the user's answer to turn_ask_human · jpeg 800×600"
            "</technical>"
        ),
        "id": "m_x",
        "attachments": [TOOL_ATT.to_dict(), ASK_ATT.to_dict()],
    }


def test_same_image_twice_in_a_batch_is_one_attachment():
    msg = build_tool_attachments_message(
        [TOOL_ATT.to_dict(), TOOL_ATT.to_dict()], message_id="m_x"
    )

    assert msg["attachments"] == [TOOL_ATT.to_dict()]
    assert msg["content"] == (
        '<technical type="technical">'
        "Image from fs_view_image: /proj/shot.png · png 1456×819"
        "</technical>"
    )


# --- батч тулов ------------------------------------------------------------


def _ptc_state():
    return SimpleNamespace(
        messages=[],
        steps=1,
        max_steps=50,
        session_compact_applied=False,
        tool_calls=0,
        turn_tool_calls=0,
        loop_detector_window=[],
        loop_detector_pending=[],
        last_unknown_tool_name=None,
        unknown_tool_repeat_count=0,
        last_internal_error_tool_name=None,
        internal_error_repeat_count=0,
        last_batch_unknown_tool_count=0,
        last_batch_invalid_arguments_count=0,
        stop_requested=False,
        pending_tool_attachments=[],
    )


def _ptc_ctx(tools_data=None):
    return fake_ctx(
        {"agent.loop_detector": {"enabled": True}},
        tools_data=tools_data or {},
        logger=None,
        run_id="run",
        subagent_progress_sink=None,
    )


def _fc(name, call_id, arguments="{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


_VIEW_ARGS = '{"path": "/proj/shot.png"}'


def _patch_batch(monkeypatch, *, attach=None, on_apply=None):
    """Двойник диспетчера: «тул картинки» кладёт вложение туда же, куда настоящий."""
    attach = attach or {}

    def fake_apply(**kw):
        name = kw["tool_name"]
        if name in attach:
            kw["state"].pending_tool_attachments.append(attach[name].to_dict())
        if on_apply is not None:
            on_apply(kw)
        return json.dumps({"tool": name, "ok": True})

    monkeypatch.setattr(pr, "aapply_tool", as_coro(fake_apply))
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res
    )
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")


def test_images_ride_after_the_loop_notice_at_the_very_end(monkeypatch):
    _patch_batch(monkeypatch, attach={"fs_view_image": TOOL_ATT})
    state = _ptc_state()
    read_args = json.dumps({"path": "a.py"})
    tools_data = {
        "fs_read_file": {
            LOOP_WARN_KEY: READ_PROFILE,
            "tool_fabric": None,
            "injected_fields": [],
        }
    }

    outs = run_coro(
        pr.aprocess_tool_calls(
            messages=[
                _fc("fs_read_file", "c1", read_args),
                _fc("fs_read_file", "c2", read_args),
                _fc("fs_view_image", "c3", _VIEW_ARGS),
            ],
            state=state,
            ctx=_ptc_ctx(tools_data),
            run_agent_func=None,
        )
    )

    assert len(outs) == 5
    assert [o["call_id"] for o in outs[:3]] == ["c1", "c2", "c3"]
    assert "Loop detector:" in outs[3]["content"]
    assert outs[4]["id"] == "m_x"
    assert outs[4]["attachments"] == [TOOL_ATT.to_dict()]
    assert state.pending_tool_attachments == []


def test_tool_and_answer_images_share_one_message(monkeypatch):
    _patch_batch(
        monkeypatch,
        attach={"fs_view_image": TOOL_ATT, "turn_ask_human": ASK_ATT},
    )
    outs = run_coro(
        pr.aprocess_tool_calls(
            messages=[
                _fc("fs_view_image", "c1", _VIEW_ARGS),
                _fc("turn_ask_human", "c2", '{"question": "Что не так?"}'),
            ],
            state=_ptc_state(),
            ctx=_ptc_ctx(),
            run_agent_func=None,
        )
    )

    tech = [o for o in outs if o.get("role") == "user"]
    assert len(tech) == 1
    assert outs[-1] is tech[0]
    assert tech[0]["attachments"] == [TOOL_ATT.to_dict(), ASK_ATT.to_dict()]


def test_batch_without_images_adds_no_message(monkeypatch):
    _patch_batch(monkeypatch)
    outs = run_coro(
        pr.aprocess_tool_calls(
            messages=[_fc("session_think", "c1")],
            state=_ptc_state(),
            ctx=_ptc_ctx(),
            run_agent_func=None,
        )
    )

    assert [o.get("type") for o in outs] == ["function_call_output"]


def test_leftover_of_an_aborted_batch_does_not_leak_into_the_next(monkeypatch):
    _patch_batch(monkeypatch)
    state = _ptc_state()
    state.pending_tool_attachments = [TOOL_ATT.to_dict()]

    outs = run_coro(
        pr.aprocess_tool_calls(
            messages=[_fc("session_think", "c1")],
            state=state,
            ctx=_ptc_ctx(),
            run_agent_func=None,
        )
    )

    assert not any("attachments" in o for o in outs)
    assert state.pending_tool_attachments == []


def test_user_stop_drops_the_images_of_the_batch(monkeypatch):
    def _stop(kw):
        kw["state"].stop_requested = True

    _patch_batch(
        monkeypatch, attach={"fs_view_image": TOOL_ATT}, on_apply=_stop
    )
    state = _ptc_state()

    outs = run_coro(
        pr.aprocess_tool_calls(
            messages=[
                _fc("fs_view_image", "c1", _VIEW_ARGS),
                _fc("session_think", "c2"),
            ],
            state=state,
            ctx=_ptc_ctx(),
            run_agent_func=None,
        )
    )

    assert [o["call_id"] for o in outs] == ["c1", "c2"]
    assert not any("attachments" in o for o in outs)
    assert state.pending_tool_attachments == []


def _agent_state(*, max_steps: int = 5) -> State:
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=max_steps,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def test_images_follow_the_session_compact_resume(monkeypatch):
    sys_m = {"role": "system", "content": "s", "id": "sys"}
    state = _agent_state(max_steps=2)
    state.backend_api_mode = "chat_completions"
    state.messages = [
        sys_m,
        {"role": "user", "content": "history", "id": "u0"},
    ]

    def _compact(kw):
        if kw["tool_name"] == "session_compact":
            state.messages = [
                sys_m,
                {"role": "user", "content": "summary", "id": "sum"},
            ]
            state.session_compact_applied = True

    _patch_batch(
        monkeypatch, attach={"fs_view_image": TOOL_ATT}, on_apply=_compact
    )

    outs = run_coro(
        pr.aprocess_tool_calls(
            [
                _fc("fs_view_image", "c_view", _VIEW_ARGS),
                _fc("session_compact", "c_compact", '{"reason": "done"}'),
            ],
            state,
            ctx=fake_ctx({}),
            run_agent_func=None,
        )
    )

    assert [m.get("call_id") for m in outs[:-2]] == ["c_view", "c_view"]
    assert "session_compact finished" in outs[-2]["content"]
    assert outs[-1]["attachments"] == [TOOL_ATT.to_dict()]


# --- граница хода: принятый turn_finish -------------------------------------


_FINISH_OK = json.dumps(
    {
        "tool": "turn_finish",
        "ok": True,
        "status": "submitted",
        "message": "Task outcome submitted.",
        "error": None,
        "task_report": "Сделано",
    },
    ensure_ascii=False,
)
_FINISH_REJECTED = json.dumps(
    {
        "tool": "turn_finish",
        "ok": False,
        "status": "rejected",
        "message": "Validation failed.",
        "error": {
            "type": "invalid_argument",
            "field": "solution_type",
            "message": "unsupported solution_type",
        },
        "task_report": "",
    },
    ensure_ascii=False,
)


def _finish_call():
    args = json.dumps(
        {
            "solution_type": "full_solution",
            "task_report": "Сделано",
            "handoff": "{}",
        },
        ensure_ascii=False,
    )
    return _fc("turn_finish", "call_finish", args)


def _agent_ctx():
    return fake_ctx(
        {
            "backend.api_mode": "chat_completions",
            "agent.strict_turn_finish_validation": False,
            "agent.no_tool_call_max_count": 5,
        },
        logger=None,
        auth_connector=None,
    )


def _patch_run(monkeypatch, *, finish_output):
    def fake_apply(**kw):
        if kw["tool_name"] == "fs_view_image":
            kw["state"].pending_tool_attachments.append(TOOL_ATT.to_dict())
            return json.dumps({"ok": True, "attached": True})
        return finish_output

    monkeypatch.setattr(pr, "aapply_tool", as_coro(fake_apply))


def test_accepted_finish_drops_the_images_with_the_tail(monkeypatch):
    _patch_run(monkeypatch, finish_output=_FINISH_OK)
    state = _agent_state()
    persisted: list = []
    finish = _finish_call()

    result = run_coro(
        pr.arun_agent(
            [_fc("fs_view_image", "c_view", _VIEW_ARGS), finish],
            state,
            _agent_ctx(),
            persist_messages_callback=persisted.extend,
        )
    )

    assert result == finish["arguments"]
    assert not any("attachments" in m for m in state.messages)
    assert not any("attachments" in m for m in persisted)
    assert state.pending_tool_attachments == []


def test_open_turn_keeps_the_images_in_the_trace(monkeypatch):
    _patch_run(monkeypatch, finish_output=_FINISH_REJECTED)
    state = _agent_state(max_steps=0)

    run_coro(
        pr.arun_agent(
            [_fc("fs_view_image", "c_view", _VIEW_ARGS), _finish_call()],
            state,
            _agent_ctx(),
        )
    )

    tech = [m for m in state.messages if "attachments" in m]
    assert [m["attachments"] for m in tech] == [[TOOL_ATT.to_dict()]]
