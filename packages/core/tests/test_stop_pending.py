"""AgentExecutor.stop_run: stop, пришедший до создания state (setup-фаза run()),
не теряется — запоминается в _stop_pending и применяется при создании state, до
первого persist. Закрывает окно off-thread сборки executor'а."""

import types

from gc_core.agent_executor import AgentExecutor


def test_stop_run_remembers_pending_when_no_state():
    ex = AgentExecutor(types.SimpleNamespace())
    assert ex._state is None
    ex.stop_run()
    assert ex._stop_pending is True


def test_stop_run_sets_state_flag_when_state_present():
    ex = AgentExecutor(types.SimpleNamespace())

    class _S:
        stop_requested = False

    ex._state = _S()
    ex.stop_run()
    assert ex._state.stop_requested is True
    assert ex._stop_pending is False
