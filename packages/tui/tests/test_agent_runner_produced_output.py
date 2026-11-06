"""AgentRunner.produced_output — флаг ядра «прогон что-то выдал» для
отката отправки. Состояние прошлого прогона на исполнителе (новое ещё не
привязано) к новому прогону не относится."""

from types import SimpleNamespace

from gc_tui.services.agent_runner import AgentRunner


class _Executor:
    def __init__(self, state):
        self.state = state

    def get_state(self):
        return self.state


def _spawned(executor) -> AgentRunner:
    runner = AgentRunner(executor=executor)
    with runner._lock:
        runner._spawn_run_locked(
            {}, thread_id="t1", start_new_thread=False, system="cli"
        )
    return runner


def test_previous_run_state_does_not_count_for_new_run():
    ex = _Executor(SimpleNamespace(produced_output=True))

    assert _spawned(ex).produced_output() is False


def test_new_run_state_flag_is_reported():
    ex = _Executor(None)
    runner = _spawned(ex)
    ex.state = SimpleNamespace(produced_output=False)
    assert runner.produced_output() is False

    ex.state.produced_output = True

    assert runner.produced_output() is True


def test_compaction_work_is_never_undoable():
    runner = AgentRunner(executor=_Executor(None))
    runner._thread_kind = "compact"

    assert runner.produced_output() is True
