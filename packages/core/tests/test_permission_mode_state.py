"""Режим разрешений в состоянии сессии: хранение, восстановление, наследование."""

import threading
from types import SimpleNamespace

import pytest

from gc_core.agent_executor import AgentExecutor
from gc_core.execution.state import SharedStateData
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    serialize_shared_block,
)
from gc_testkit.session_meta import shared_from_meta
from gc_core.security.permission_mode import Mode, resolve_mode_with_source
from gc_core.security.permission_store import PermissionStore
from gc_testkit.settings import fake_ctx


def test_shared_state_has_no_mode_until_one_is_chosen():
    assert SharedStateData().permission_mode is None
    # «выбора не было» — отсутствие ключа: мета такой сессии не меняется
    assert "permission_mode" not in serialize_shared_block(SharedStateData())


def test_mode_survives_save_and_restore():
    shared = SharedStateData(permission_mode="manual")
    block = serialize_shared_block(shared)
    assert block["permission_mode"] == "manual"
    restored = shared_from_meta(
        {"state_schema_version": STATE_SCHEMA_VERSION, "shared": block}
    )
    assert restored.permission_mode == "manual"


def test_session_saved_before_the_feature_restores_without_a_mode():
    block = serialize_shared_block(SharedStateData())
    assert "permission_mode" not in block
    block["notes_md"] = "x"
    restored = shared_from_meta(
        {"state_schema_version": STATE_SCHEMA_VERSION, "shared": block}
    )
    assert restored.permission_mode is None


@pytest.mark.parametrize("raw", ["", "   ", None, 7, ["manual"]])
def test_garbage_in_meta_is_not_a_mode(raw):
    block = serialize_shared_block(SimpleNamespace(permission_mode=raw))
    assert "permission_mode" not in block


def test_store_reads_and_writes_the_mode_through_shared_state():
    shared = SharedStateData()
    store = PermissionStore(shared.confirm_rules, shared=shared)
    assert store.permission_mode() is None
    store.set_permission_mode("manual")
    assert shared.permission_mode == "manual"
    assert store.permission_mode() == "manual"


def test_store_without_shared_state_has_no_mode():
    store = PermissionStore()
    store.set_permission_mode("manual")  # некуда писать — не падает
    assert store.permission_mode() is None


def test_subagent_inherits_the_mode_through_the_shared_store():
    """Стор у субагента тот же объект, shared — тот же: режим родителя виден
    ребёнку без проводов, и смена посреди прогона доезжает сразу."""
    shared = SharedStateData(permission_mode="manual")
    store = PermissionStore(shared.confirm_rules, shared=shared)
    parent = fake_ctx(permission_store=store)
    child = fake_ctx(permission_store=parent.permission_store)
    assert resolve_mode_with_source(child) == (Mode.MANUAL, "session")
    shared.permission_mode = "edit"
    assert resolve_mode_with_source(child) == (Mode.EDIT, "session")


# --- исполнитель: слой явного выбора ---------------------------------------------


def _executor():
    ex = AgentExecutor.__new__(AgentExecutor)
    ex._lock = threading.Lock()
    ex._state = None
    ex._stop_pending = False
    ex._permission_mode_choice = None
    return ex


def _state(mode=None):
    return SimpleNamespace(
        shared=SharedStateData(permission_mode=mode),
        discard_writes_after_stop=False,
        stop_requested=False,
    )


def test_choice_made_before_a_run_lands_on_the_new_state():
    ex = _executor()
    ex.set_permission_mode("manual")
    state = _state(mode="edit")  # в сессии сохранён другой
    ex._bind_state(state, discard_writes_after_stop=False)
    assert state.shared.permission_mode == "manual"


def test_saved_session_mode_stays_when_nothing_was_chosen():
    ex = _executor()
    state = _state(mode="manual")
    ex._bind_state(state, discard_writes_after_stop=False)
    assert state.shared.permission_mode == "manual"
    assert ex.permission_mode() == "manual"


def test_switch_mid_run_reaches_the_live_state():
    ex = _executor()
    state = _state()
    ex._bind_state(state, discard_writes_after_stop=False)
    ex.set_permission_mode("manual")
    assert state.shared.permission_mode == "manual"


def test_choice_survives_into_the_next_run():
    ex = _executor()
    ex._bind_state(_state(), discard_writes_after_stop=False)
    ex.set_permission_mode("manual")
    next_state = _state(mode="edit")
    ex._bind_state(next_state, discard_writes_after_stop=False)
    assert next_state.shared.permission_mode == "manual"


def test_unknown_mode_is_rejected_at_the_writer():
    ex = _executor()
    with pytest.raises(ValueError):
        ex.set_permission_mode("bypass")
    assert ex.permission_mode() is None


def test_cleared_choice_lets_the_session_keep_its_own():
    """Окно открыло сессию без режима: выбор снят, следующий прогон берёт
    режим из сессии (там его нет — из конфига), а не прежний выбор окна."""
    ex = _executor()
    ex.set_permission_mode("auto")
    ex.clear_permission_mode()
    assert ex.permission_mode() is None
    state = _state(mode=None)
    ex._bind_state(state, discard_writes_after_stop=False)
    assert state.shared.permission_mode is None
