"""AgentRunner.start прокидывает технический компаньон вторым user-сообщением."""

import time

from gc_tui.services.agent_runner import AgentRunner


class _CaptureExecutor:
    def __init__(self):
        self.calls = []

    async def arun(self, messages, **kwargs):
        self.calls.append((list(messages), kwargs.get("message_ids")))

    def stop_run(self):
        pass

    def add_message(self, m):
        pass


def _wait_idle(runner):
    for _ in range(100):
        if not runner.is_running():
            return
        time.sleep(0.02)


def test_start_without_technical_sends_single_message():
    ex = _CaptureExecutor()
    r = AgentRunner(executor=ex)
    r.start(query="привет", thread_id="t1", message_id="m1")
    _wait_idle(r)
    messages, mids = ex.calls[0]
    assert messages == ["привет"]
    assert mids == ["m1"]


def test_start_with_technical_sends_pair():
    ex = _CaptureExecutor()
    r = AgentRunner(executor=ex)
    r.start(
        query="почини баг",
        technical='<technical type="technical">attached</technical>',
        thread_id="t1",
        message_id="m1",
    )
    _wait_idle(r)
    messages, mids = ex.calls[0]
    assert messages == [
        "почини баг",
        '<technical type="technical">attached</technical>',
    ]
    assert mids == ["m1", None]
