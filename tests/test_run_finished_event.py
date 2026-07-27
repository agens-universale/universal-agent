import logging


class _LoggerSpy(logging.Logger):
    def __init__(self):
        super().__init__("spy")
        self.events = []

    def info(self, msg, *args, **kwargs):
        extra = kwargs.get("extra") or {}
        if extra.get("event"):
            self.events.append(extra)


def test_run_finished_payload_shape():
    """Событие run_finished обязано нести исход рана в машиночитаемом виде.

    Сегодня terminal_outcome читает только TUI, а gc-cli всегда возвращает 0,
    поэтому headless-оркестратор не отличает финиш от упора в лимит.
    """
    from agent_executor import _emit_run_finished

    logger = _LoggerSpy()
    _emit_run_finished(
        logger,
        terminal_outcome="step_limit",
        steps=12,
        tool_calls=34,
        thread_id="t1",
        system="cli",
    )
    assert len(logger.events) == 1
    event = logger.events[0]
    assert event["event"] == "run_finished"
    assert event["terminal_outcome"] == "step_limit"
    assert event["steps"] == 12
    assert event["tool_calls"] == 34
    assert event["thread_id"] == "t1"
    assert event["system"] == "cli"


def test_run_finished_survives_a_broken_logger():
    from agent_executor import _emit_run_finished

    class _Boom:
        def __init__(self):
            self.calls = 0

        def info(self, *a, **k):
            self.calls += 1
            raise RuntimeError("boom")

    boom = _Boom()
    # Отчётность не должна ронять ран: исключение логгера гасится, но попытка
    # записи была — иначе тест прошёл бы и на пустой реализации.
    _emit_run_finished(boom, terminal_outcome=None, steps=1, tool_calls=0,
                       thread_id=None, system="cli")
    assert boom.calls == 1


def test_none_logger_is_a_noop():
    from agent_executor import _emit_run_finished

    # None-логгер — штатный случай: logger может отсутствовать на ctx.
    assert _emit_run_finished(None, terminal_outcome=None, steps=0,
                              tool_calls=0, thread_id=None, system="cli") is None
