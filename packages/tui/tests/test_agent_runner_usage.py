"""agent_runner.usage_snapshot: делегирует usage_meta_snapshot() executor'у.

Если executor ещё не построен (factory не отработала) — None, без сборки.
"""
from gc_tui.services.agent_runner import AgentRunner


class _UsageExecutor:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def usage_meta_snapshot(self):
        return self._snapshot


def test_none_when_executor_not_built():
    built = []

    def factory():
        built.append(True)
        return _UsageExecutor(({"by_category": {}}, None))

    runner = AgentRunner(executor_factory=factory)
    assert runner.usage_snapshot() is None
    assert built == []  # не должен строить executor ради снапшота


def test_delegates_to_executor_snapshot():
    snap = ({"by_category": {"main": {"input": 10}}}, None)
    runner = AgentRunner(executor=_UsageExecutor(snap))
    assert runner.usage_snapshot() == snap
