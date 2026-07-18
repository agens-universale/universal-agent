"""Unit: sidecar records → nested mount actions."""

from tui.services.subagent_sidecar_mapper import (
    CHILD_SUMMARY_LABEL,
    apply_sidecar_nested_actions,
    plan_sidecar_nested_actions,
)


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
