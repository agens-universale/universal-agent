"""Судья действий режима auto: что уходит модели и как читается её ответ."""

import json

import pytest

from gc_core.security.action_judge import (
    ACTION_RECORD_CHARS,
    HISTORY_RECORDS,
    JUDGED_ACTION_CHARS,
    MESSAGE_CHARS,
    ActionJudgeMalformed,
    ConversationEntry,
    JudgeRequest,
    action_blocked,
    build_action_judge_messages,
    build_action_judge_payload,
    parse_action_verdict,
)


def _request(**overrides):
    base = dict(
        conversation=(ConversationEntry.user("почини тесты"),),
        tool="exec_command",
        category="exec",
        arguments={"command": "pytest -q"},
    )
    base.update(overrides)
    return JudgeRequest(**base)


def test_payload_keeps_the_order_and_puts_the_action_last():
    payload = build_action_judge_payload(
        _request(
            conversation=(
                ConversationEntry.user("почини тесты"),
                ConversationEntry.agent("Запушить в main?"),
                ConversationEntry.user("да"),
                ConversationEntry.action(
                    "exec_command", {"command": "git status"}, "ok"
                ),
            ),
            flags=("outside_project",),
            runtime={"project_dir": "/p"},
        )
    )
    assert list(payload) == ["runtime", "conversation", "flags", "action"]
    assert payload["conversation"] == [
        {"user": "почини тесты"},
        {"agent": "Запушить в main?"},
        {"user": "да"},
        {
            "action": {
                "tool": "exec_command",
                "arguments": {"command": "git status"},
                "outcome": "ok",
            }
        },
    ]
    assert payload["action"] == {
        "tool": "exec_command",
        "category": "exec",
        "arguments": {"command": "pytest -q"},
    }
    assert payload["flags"] == ["outside_project"]


def test_plan_and_delegated_task_appear_only_when_present():
    assert "approved_plan" not in build_action_judge_payload(_request())
    payload = build_action_judge_payload(
        _request(approved_plan="1. шаг", delegated_task="собери отчёт")
    )
    assert payload["approved_plan"] == "1. шаг"
    assert payload["delegated_task"] == "собери отчёт"
    assert list(payload).index("conversation") > list(payload).index(
        "delegated_task"
    )


def test_long_parts_are_cut_with_an_explicit_marker():
    payload = build_action_judge_payload(
        _request(
            conversation=(
                ConversationEntry.user("я" * (MESSAGE_CHARS + 50)),
                ConversationEntry.action(
                    "fs_write_file",
                    {
                        "path": "a.py",
                        "content": "x" * (ACTION_RECORD_CHARS * 2),
                    },
                    "ok",
                ),
            ),
            arguments={"command": "echo " + "y" * (JUDGED_ACTION_CHARS * 2)},
        )
    )
    user, action = payload["conversation"]
    assert (
        len(user["user"]) < MESSAGE_CHARS + 40
        and "[TEXT CUTTED]" in user["user"]
    )
    earlier = action["action"]
    assert earlier["arguments_truncated"] is True
    assert len(earlier["arguments"]) < ACTION_RECORD_CHARS + 40
    assert payload["action"]["arguments_truncated"] is True
    judged = payload["action"]["arguments"]
    assert len(judged) < JUDGED_ACTION_CHARS + 40
    # у проверяемого действия вырезается середина: цель команды чаще в конце
    assert judged.startswith('{"command": "echo yyy') and judged.endswith(
        'yyy"}'
    )
    assert "[TEXT CUTTED]" in judged


def test_old_history_is_dropped_from_the_start_and_counted():
    entries = tuple(
        ConversationEntry.action(
            "exec_command", {"command": f"step {i}"}, "ok"
        )
        for i in range(HISTORY_RECORDS + 5)
    )
    payload = build_action_judge_payload(
        _request(conversation=(ConversationEntry.user("задача"), *entries))
    )
    conversation = payload["conversation"]
    # первое сообщение пользователя остаётся, выпавшие записи посчитаны
    assert conversation[0] == {"user": "задача"}
    assert conversation[1] == {"omitted_earlier_entries": 5}
    assert len(conversation) == 2 + HISTORY_RECORDS
    assert conversation[-1]["action"]["arguments"] == {
        "command": f"step {HISTORY_RECORDS + 4}"
    }


def test_messages_carry_the_bundled_instruction_and_the_payload():
    system, user = build_action_judge_messages(
        build_action_judge_payload(_request())
    )
    assert (
        system["role"] == "system" and "crosses_boundary" in system["content"]
    )
    assert user["role"] == "user"
    assert '"command": "pytest -q"' in user["content"]
    assert "{judge_payload_json}" not in user["content"]


@pytest.mark.parametrize(
    "fields, blocked",
    [
        ((False, False, False), False),
        ((False, True, True), False),
        ((False, True, False), True),
        ((True, False, False), True),
        ((True, True, True), True),
    ],
)
def test_decision_is_derived_by_the_program(fields, blocked):
    crosses, risky, authorized = fields
    verdict = parse_action_verdict(
        json.dumps(
            {
                "crosses_boundary": crosses,
                "risky": risky,
                "authorized": authorized,
                "reason": "причина",
            }
        )
    )
    assert (
        action_blocked(verdict, build_action_judge_payload(_request()))
        is blocked
    )
    assert verdict.reason == "причина"


def test_verdict_survives_think_tags_and_code_fences():
    raw = (
        "<think>hmm</think>\n```json\n"
        '{"crosses_boundary": false, "risky": true, "authorized": false, "reason": "r"}'
        "\n```"
    )
    verdict = parse_action_verdict(raw)
    assert (verdict.crosses_boundary, verdict.risky, verdict.authorized) == (
        False,
        True,
        False,
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "allow",
        '{"risky": false}',
        '{"crosses_boundary": "no", "risky": false, "authorized": false}',
        '{"crosses_boundary": false, "risky": 0, "authorized": false}',
        '["crosses_boundary"]',
    ],
)
def test_anything_but_three_booleans_is_not_a_verdict(raw):
    """Fail-closed: негодный ответ — не разрешение и не отказ, а сбой проверки."""
    with pytest.raises(ActionJudgeMalformed):
        parse_action_verdict(raw)


def _verdict(*, risky, authorized, crosses=False):
    return parse_action_verdict(
        json.dumps(
            {
                "crosses_boundary": crosses,
                "risky": risky,
                "authorized": authorized,
            }
        )
    )


@pytest.mark.parametrize("flag", ["protected_path", "restricted_address"])
def test_strict_flag_makes_the_action_risky_whatever_the_model_says(flag):
    """Модель ответила «не рискованно», а программа пометила действие: без
    разрешения пользователя оно не проходит."""
    payload = build_action_judge_payload(_request(flags=(flag,)))
    assert (
        action_blocked(_verdict(risky=False, authorized=False), payload)
        is True
    )
    assert (
        action_blocked(_verdict(risky=False, authorized=True), payload)
        is False
    )


def test_outside_project_flag_alone_changes_nothing():
    payload = build_action_judge_payload(_request(flags=("outside_project",)))
    assert (
        action_blocked(_verdict(risky=False, authorized=False), payload)
        is False
    )


def test_truncated_risky_action_is_blocked_even_when_called_authorized():
    """Обрезка скрывает часть действия: проверить, что разрешение покрывает
    невидимое, нельзя. Безобидное действие обрезка не блокирует."""
    payload = build_action_judge_payload(
        _request(
            arguments={"command": "rsync " + "x" * (JUDGED_ACTION_CHARS * 2)}
        )
    )
    assert (
        action_blocked(_verdict(risky=True, authorized=True), payload) is True
    )
    assert (
        action_blocked(_verdict(risky=False, authorized=False), payload)
        is False
    )
