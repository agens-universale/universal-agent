"""Unit: sidecar records → nested mount actions."""

import json

from gc_tui.services.subagent_sidecar_mapper import CHILD_SUMMARY_LABEL, SidecarNestedAction, apply_sidecar_nested_actions, plan_sidecar_nested_actions


def test_plan_groups_by_parent_and_seq():
    records = [
        {
            "seq": 2,
            "parent_call_id": "p1",
            "kind": "result",
            "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        },
        {
            "seq": 1,
            "parent_call_id": "p1",
            "kind": "call",
            "payload": {
                "type": "function_call",
                "name": "fs_read_file",
                "arguments": '{"path": "a.py"}',
                "call_id": "c1",
            },
        },
        {
            "seq": 3,
            "parent_call_id": "p1",
            "kind": "child_summary",
            "payload": {"synthesis": "…"},
        },
        {
            "seq": 4,
            "parent_call_id": "p1",
            "kind": "finished",
            "payload": {"ok": True},
        },
    ]
    actions = plan_sidecar_nested_actions(records)
    assert [a.kind for a in actions] == [
        "mount_nested_tool",
        "finish_nested_tool",
        "mount_nested_summary",
    ]
    assert actions[0].tool_name == "fs_read_file"
    assert "a.py" in (actions[0].args_summary or "")
    assert actions[1].ok is True
    assert actions[2].prose == CHILD_SUMMARY_LABEL


def test_plan_user_kind_mounts_nested_user():
    actions = plan_sidecar_nested_actions(
        [
            {
                "seq": 1,
                "parent_call_id": "p1",
                "kind": "user",
                "payload": {"role": "user", "content": "steer here"},
            }
        ]
    )
    assert len(actions) == 1
    assert actions[0].kind == "mount_nested_user"
    assert actions[0].prose == "steer here"


def test_apply_calls_hooks_in_order():
    seen = []
    actions = plan_sidecar_nested_actions(
        [
            {
                "seq": 1,
                "parent_call_id": "p1",
                "kind": "call",
                "payload": {
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "c_err",
                    "arguments": "{}",
                },
            },
            {
                "seq": 2,
                "parent_call_id": "p1",
                "kind": "intermediate_reply",
                "payload": {"prose": "hi", "call_id": "ir1"},
            },
            {
                "seq": 3,
                "parent_call_id": "p1",
                "kind": "error",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "c_err",
                    "output": '{"ok": false, "error": {"message": "boom"}}',
                },
            },
        ]
    )
    apply_sidecar_nested_actions(
        actions,
        mount_nested_tool=lambda **kw: seen.append(("tool", kw["tool_call_id"])),
        finish_nested_tool=lambda **kw: seen.append(
            ("finish", kw["tool_call_id"], kw["ok"])
        ),
        mount_nested_prose=lambda **kw: seen.append(("prose", kw["prose"])),
        mount_nested_summary=lambda **kw: seen.append(("summary", kw["prose"])),
    )
    assert seen == [
        ("tool", "c_err"),
        ("prose", "hi"),
        ("finish", "c_err", False),
    ]


def _todo_call(pid, cid, seq, todos):
    # arguments — JSON-строка: реальная форма function_call в data_subagents_*.jsonl
    # (processing_responses кладёт payload=dict(m) с сырыми аргументами модели).
    return {"parent_call_id": pid, "seq": seq, "kind": "call",
            "payload": {"call_id": cid, "name": "session_todo",
                        "arguments": json.dumps({"todos": todos}, ensure_ascii=False)}}


def test_session_todo_first_call_plans_nested_todo_action():
    todos = [{"content": "a", "status": "pending"}]
    actions = plan_sidecar_nested_actions([_todo_call("p1", "c1", 1, todos)])
    todo_actions = [a for a in actions if a.kind == "mount_nested_todo"]
    assert len(todo_actions) == 1
    assert todo_actions[0].todo_items == todos
    assert todo_actions[0].tool_call_id == "c1"


def test_session_todo_every_call_plans_nested_todo_action():
    """v3 спеки: каждый вызов — развёрнутым, включая смену только статусов
    и пустой список (рендерится строкой «чек-лист субагента очищен»)."""
    stream = [
        [{"content": "a", "status": "pending"}],
        [{"content": "a", "status": "completed"}],                   # статусы
        [{"content": "a", "status": "completed"},
         {"content": "b", "status": "pending"}],                     # состав
        [],                                                          # очистка
    ]
    records = [_todo_call("p1", f"c{i}", i, todos)
               for i, todos in enumerate(stream, start=1)]
    actions = plan_sidecar_nested_actions(records)
    assert [a.kind for a in actions] == ["mount_nested_todo"] * 4
    assert actions[-1].todo_items == []


def test_apply_routes_todo_action_to_callback():
    calls = []
    action = SidecarNestedAction(
        kind="mount_nested_todo", parent_call_id="p1", tool_call_id="c1",
        todo_items=[{"content": "a", "status": "pending"}])
    apply_sidecar_nested_actions(
        [action],
        mount_nested_tool=lambda **kw: calls.append(("tool", kw)),
        finish_nested_tool=lambda **kw: None,
        mount_nested_prose=lambda **kw: None,
        mount_nested_todo=lambda **kw: calls.append(("todo", kw)),
    )
    assert calls[0][0] == "todo"
    assert calls[0][1]["todo_items"][0]["content"] == "a"


def test_live_and_sidecar_channels_agree_on_todo_routing():
    """Инвариант: live (apply_drained) и sidecar (plan) маршрутизируют
    одинаково — v3: каждый вызов session_todo обоими каналами → todo."""
    from gc_core.tools.subagents.progress import SubagentProgressEvent
    from gc_tui.services.subagent_progress import SubagentProgressConsumer

    stream = [
        [{"content": "a", "status": "pending"}],
        [{"content": "a", "status": "completed"}],
        [],
    ]
    records = [_todo_call("p1", f"c{i}", i, todos)
               for i, todos in enumerate(stream, start=1)]
    sidecar_kinds = [
        "todo" if a.kind == "mount_nested_todo" else "tool"
        for a in plan_sidecar_nested_actions(records)
    ]

    events = [
        SubagentProgressEvent(
            parent_call_id="p1", child_run_id="child_1", kind="tool_start",
            step=i, max_steps=50, elapsed_ms=0, tool_name="session_todo",
            tool_call_id=f"c{i}", todo_items=todos,
        )
        for i, todos in enumerate(stream, start=1)
    ]
    fired: list[str] = []
    SubagentProgressConsumer().apply_drained(
        events,
        mount_nested_tool=lambda **kw: fired.append("tool"),
        finish_nested_tool=lambda **kw: None,
        mount_nested_prose=lambda **kw: None,
        parent_heartbeat=lambda **kw: None,
        mount_nested_todo=lambda **kw: fired.append("todo"),
    )

    assert sidecar_kinds == fired == ["todo", "todo", "todo"]


def test_result_for_expanded_todo_call_is_noop():
    """finish по развёрнутому: todo-строка не регистрируется как tool-row —
    результат для её call_id не должен ничего монтировать/менять."""
    todos = [{"content": "a", "status": "pending"}]
    records = [
        _todo_call("p1", "c1", 1, todos),
        {"parent_call_id": "p1", "seq": 2, "kind": "result",
         "payload": {"call_id": "c1"}},
    ]
    actions = plan_sidecar_nested_actions(records)
    finish = [a for a in actions if a.kind == "finish_nested_tool"]
    # finish-action планируется (generic-путь), но его tool_call_id не был
    # смонтирован как tool-row → apply-колбэк main его молча игнорирует.
    # Здесь фиксируем только, что todo-действие есть и порядок не сломан.
    assert [a.kind for a in actions][:1] == ["mount_nested_todo"]
    assert len(finish) == 1


def test_session_todo_garbage_arguments_plan_generic_tool_row():
    """Битые аргументы -> НЕ чек-лист: обычная tool-строка (статус вызова
    виден механикой), «чек-лист очищен» не рисуем."""
    rec = {"parent_call_id": "p1", "seq": 1, "kind": "call",
           "payload": {"call_id": "c1", "name": "session_todo",
                       "arguments": "оборванный json{{{"}}
    actions = plan_sidecar_nested_actions([rec])
    assert [a.kind for a in actions] == ["mount_nested_tool"]
    assert "очищен" not in (actions[0].args_summary or "")
