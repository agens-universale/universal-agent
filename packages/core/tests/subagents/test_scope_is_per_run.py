"""Ввод пользователя не может уехать в чужую сессию.

Процесс ведёт МНОГО прогонов сразу: сервер держит их в
`AgentRunRegistry._runs` и аккуратно находит нужный по ключу
`(system, task_id)`. Раньше вся эта адресация тут же терялась: скоуп ввода жил
в ГЛОБАЛЬНОЙ переменной модуля, и `add_message` отдавал сообщение тому
субагенту, кто зарегистрировался последним — чужому в том числе.

Два сценария ниже — ровно те, что воспроизводились на глобали.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from gc_core.agent_executor import AgentExecutor
from gc_core.tools.subagents.active_scope import (
    activate_child_input,
    is_child_input_active,
    try_enqueue_user_message,
)


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(child_input_scope=None)


def test_message_of_one_run_never_reaches_another_runs_child():
    """Сообщение сессии A уходит субагенту A, даже когда работает и B."""
    a, b = _ctx(), _ctx()

    with activate_child_input(a, parent_call_id="call_A") as scope_a:
        with activate_child_input(b, parent_call_id="call_B") as scope_b:
            assert try_enqueue_user_message(
                a, {"role": "user", "content": "для A"}
            )
            assert try_enqueue_user_message(
                b, {"role": "user", "content": "для B"}
            )

            assert scope_a.drain() == [{"role": "user", "content": "для A"}]
            assert scope_b.drain() == [{"role": "user", "content": "для B"}]


def test_finished_run_does_not_unregister_a_live_one():
    """Выход B не снимает регистрацию A: тот продолжает получать свой ввод.

    На глобали второй `activate_child_input` затирал первый, а на выходе
    обнулял регистрацию целиком — A оставался работать незарегистрированным, и
    его сообщения уходили родителю. Молча и в обе стороны.
    """
    a, b = _ctx(), _ctx()

    with activate_child_input(a, parent_call_id="call_A") as scope_a:
        with activate_child_input(b, parent_call_id="call_B"):
            pass

        assert is_child_input_active(a) is True, (
            "A потерял регистрацию из-за B"
        )
        assert try_enqueue_user_message(
            a, {"role": "user", "content": "для A"}
        )
        assert scope_a.drain() == [{"role": "user", "content": "для A"}]

    assert is_child_input_active(b) is False


def test_executor_addresses_its_own_run():
    """`add_message` адресует по СВОЕМУ контексту, а не по состоянию процесса.

    Исполнитель сессии B кладёт сообщение в очередь родителя, пока субагент
    работает в сессии A, — именно это раньше и утекало.
    """
    a = _ctx()
    b_ctx = MagicMock()
    b_ctx.child_input_scope = None
    ex_b = AgentExecutor(b_ctx)

    with activate_child_input(a, parent_call_id="call_A") as scope_a:
        ex_b.add_message({"role": "user", "content": "для B"})

        assert scope_a.drain() == [], (
            "сообщение сессии B ушло субагенту сессии A"
        )
        with ex_b.get_lock():
            assert [m["content"] for m in ex_b._pending_messages] == ["для B"]
        assert ex_b.is_child_input_active() is False
