"""Заголовок блока `Agent`: роль субагента и задача из сырых аргументов
вызова (решение владельца 2026-10-23, спека P4 §7)."""

import json

import pytest

from gc_tui.models import AgentHead
from gc_tui.services.agent_head import agent_head
from gc_tui.services.trace_mapper import map_message


@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("subagent_worker", "Worker"),
        ("subagent_code_reviewer", "Reviewer"),
        ("subagent_code_generator", "Coder"),
        ("subagent_security_audit", "Security audit"),
    ],
)
def test_role_by_tool_name(name, role):
    args = json.dumps({"reason": "r", "task": "проверь клиент"})
    assert agent_head(name, args) == AgentHead(role, "проверь клиент")


def test_special_agent_role_from_argument():
    args = {"role": "Аудитор ретраев", "task": "сверь таблицу"}
    assert agent_head("subagent_special_agent", args) == AgentHead(
        "Аудитор ретраев", "сверь таблицу"
    )


def test_special_agent_without_role():
    assert agent_head("subagent_special_agent", "{}") == AgentHead(
        "Special agent", ""
    )


def test_not_a_subagent():
    assert agent_head("fs_read_file", {"path": "a.py"}) is None


def test_broken_arguments_keep_role():
    assert agent_head("subagent_worker", "{не json") == AgentHead("Worker", "")


def test_mapper_carries_head():
    msg = {
        "type": "function_call",
        "name": "subagent_critic",
        "call_id": "c1",
        "arguments": json.dumps({"task": "найди дыры"}),
    }
    ev = map_message(msg, no=1, ts="t")
    assert ev.agent == AgentHead("Critic", "найди дыры")
