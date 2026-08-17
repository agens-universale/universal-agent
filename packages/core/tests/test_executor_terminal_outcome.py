from types import SimpleNamespace
from gc_core.agent_executor import AgentExecutor

def test_executor_inits_last_terminal_outcome():
    # AgentExecutor.__init__(self, ctx) только сохраняет ctx и заводит локи —
    # лёгкий SimpleNamespace в роли ctx достаточен.
    ex = AgentExecutor(SimpleNamespace())
    assert ex.last_terminal_outcome is None
