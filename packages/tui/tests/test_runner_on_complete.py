from gc_tui.services.agent_runner import AgentRunner


class _Exec:
    last_terminal_outcome = "step_limit"

    def add_message(self, m):
        pass

    async def arun(self, *a, **k):
        return "done"

    def stop_run(self):
        pass


def test_on_complete_receives_outcome():
    got = {}
    r = AgentRunner(executor=_Exec(), on_complete=lambda o: got.setdefault("o", o))
    r.start(query="q", thread_id="t")
    r.wait(timeout=5)
    assert got.get("o") == "step_limit"
