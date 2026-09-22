"""agent_runner: бэкенд-агностичная обёртка над AgentExecutor."""
import threading
import time

import pytest

from gc_tui.services.agent_runner import AgentRunner


class _FakeExecutor:
    """Имитация AgentExecutor: run блокирует, stop_run выставляет флаг."""

    def __init__(self):
        self.run_called_with = None
        self.stopped = False
        self.messages_added: list[dict] = []
        self._event = threading.Event()
        self.started = threading.Event()   # выставляется, когда run реально начался

    async def arun(self, messages, *, thread_id, start_new_thread=False,
            system=None, verbose=None, discard_writes_after_stop=False,
            message_ids=None, display_contents=None, skills_loaded=None):
        self.run_called_with = {
            "messages": messages, "thread_id": thread_id,
            "start_new_thread": start_new_thread, "system": system,
        }
        self.started.set()   # детерминированный сигнал вместо sleep в тестах
        self._event.wait(timeout=2.0)
        return None

    def stop_run(self):
        self.stopped = True
        self._event.set()

    def add_message(self, message):
        self.messages_added.append(message)


def test_runner_starts_in_background_and_stops():
    fake = _FakeExecutor()
    runner = AgentRunner(executor=fake)
    runner.start(query="привет", thread_id="t1")
    assert fake.started.wait(2.0)          # ждём реального старта, без гонки sleep
    assert fake.run_called_with is not None
    assert fake.run_called_with["messages"] == ["привет"]
    assert fake.run_called_with["thread_id"] == "t1"
    assert runner.is_running()
    runner.stop()
    runner.wait(timeout=1.0)
    assert fake.stopped is True
    assert not runner.is_running()


def test_runner_add_message_passes_through():
    fake = _FakeExecutor()
    runner = AgentRunner(executor=fake)
    runner.start(query="x", thread_id="t1")
    runner.add_message({"role": "user", "content": "ещё"})
    assert fake.messages_added == [{"role": "user", "content": "ещё"}]
    runner.stop()
    runner.wait(timeout=1.0)


def test_runner_double_start_rejected():
    fake = _FakeExecutor()
    runner = AgentRunner(executor=fake)
    assert runner.start(query="a", thread_id="t1") is True
    assert runner.start(query="b", thread_id="t2") is False
    runner.stop()
    runner.wait(timeout=1.0)


def test_submit_ask_human_reply_terminates_real_read_loop():
    """Fake executor running ask_human's actual read-loop must complete with the answer."""
    from gc_tui.services.agent_runner import AgentRunner

    captured = {}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            # Воспроизводим точную логику ask_human.__call__ read-loop
            lines = []
            while True:
                line = input()
                if line == "":
                    break
                lines.append(line)
            captured["response"] = "\n".join(lines).strip()
        def stop_run(self): pass
        def add_message(self, m): pass

    runner = AgentRunner(executor=_Fake())
    runner.start(query="hi", thread_id="t1")
    runner.submit_ask_human_reply("user answer")
    runner.wait(timeout=2.0)
    assert captured["response"] == "user answer"


def test_cancel_ask_human_yields_empty_response():
    """cancel → пустая строка → read-loop ломается сразу → пустой ответ."""
    from gc_tui.services.agent_runner import AgentRunner

    captured = {}

    class _Fake:
        async def arun(self, messages, *, thread_id, **kw):
            lines = []
            while True:
                line = input()
                if line == "":
                    break
                lines.append(line)
            captured["response"] = "\n".join(lines).strip()
        def stop_run(self): pass
        def add_message(self, m): pass

    runner = AgentRunner(executor=_Fake())
    runner.start(query="hi", thread_id="t1")
    runner.cancel_ask_human()
    runner.wait(timeout=2.0)
    assert captured["response"] == ""


def test_start_passes_message_id_as_message_ids():
    """message_id → executor.run(message_ids=[message_id]) (для отката реплики по id)."""
    seen = []

    class _Stub:
        async def arun(self, messages, *, thread_id, start_new_thread=False, system=None,
                verbose=None, discard_writes_after_stop=False, message_ids=None,
                display_contents=None, skills_loaded=None):
            seen.append(message_ids)

        def stop_run(self): pass
        def add_message(self, m): pass

    runner = AgentRunner(executor=_Stub())
    runner.start(query="hi", thread_id="t1", system="cli", message_id="m_abc")
    runner.wait(timeout=2.0)
    assert seen == [["m_abc"]]


@pytest.mark.asyncio
async def test_runner_start_passes_discard_writes_after_stop_kwarg():
    """AgentRunner.start вызывает executor.run с discard_writes_after_stop=True."""
    from gc_tui.services.agent_runner import AgentRunner

    calls = []
    class _StubExec:
        async def arun(self, messages, *, thread_id, start_new_thread, system,
                discard_writes_after_stop=False, message_ids=None,
                display_contents=None, skills_loaded=None):
            calls.append({
                "messages": messages,
                "thread_id": thread_id,
                "start_new_thread": start_new_thread,
                "system": system,
                "discard_writes_after_stop": discard_writes_after_stop,
            })

    runner = AgentRunner(executor=_StubExec())
    started = runner.start(query="hello", thread_id="t1", system="cli")
    assert started is True
    runner.wait(timeout=2)
    assert len(calls) == 1
    assert calls[0]["discard_writes_after_stop"] is True


def test_run_exception_calls_on_error_with_exception():
    """executor.run бросает → on_error-callback получает сам объект исключения."""
    seen: list[BaseException] = []
    called = threading.Event()

    class _Boom:
        async def arun(self, messages, *, thread_id, **kw):
            raise RuntimeError("упало в run")
        def stop_run(self): pass
        def add_message(self, m): pass

    def on_error(exc):
        seen.append(exc)
        called.set()

    runner = AgentRunner(executor=_Boom(), on_error=on_error)
    assert runner.start(query="x", thread_id="t1") is True
    assert called.wait(timeout=2.0), "on_error не вызван"
    runner.wait(timeout=2.0)
    assert len(seen) == 1
    assert isinstance(seen[0], RuntimeError)
    assert str(seen[0]) == "упало в run"
    assert not runner.is_running()


def test_marked_stopped_skips_on_error():
    """После replace (_marked_stopped) хвостовой Exception не зовёт on_error —
    иначе ✗ Ошибка залипает поверх нового живого прогона."""
    seen: list[BaseException] = []
    entered = threading.Event()
    release = threading.Event()

    class _BoomAfterHold:
        async def arun(self, messages, *, thread_id, **kw):
            entered.set()
            assert release.wait(timeout=5.0)
            raise RuntimeError("хвост после replace")
        def stop_run(self): pass
        def add_message(self, m): pass

    def on_error(exc):
        seen.append(exc)

    runner = AgentRunner(executor=_BoomAfterHold(), on_error=on_error)
    assert runner.start(query="x", thread_id="t1") is True
    assert entered.wait(timeout=2.0)
    runner.stop()
    runner._marked_stopped = True
    release.set()
    runner.wait(timeout=2.0)
    assert seen == []


def test_stop_requested_skips_on_error():
    """stop() без mark: исключение после stop_requested не пишет on_error
    (иначе /stop превращается в ✗ Ошибка вместо ■ Отменено)."""
    seen: list[BaseException] = []
    entered = threading.Event()
    release = threading.Event()

    class _BoomAfterHold:
        async def arun(self, messages, *, thread_id, **kw):
            entered.set()
            assert release.wait(timeout=5.0)
            raise RuntimeError("ошибка во время stop")
        def stop_run(self): pass
        def add_message(self, m): pass

    runner = AgentRunner(
        executor=_BoomAfterHold(),
        on_error=lambda exc: seen.append(exc),
    )
    assert runner.start(query="x", thread_id="t1") is True
    assert entered.wait(timeout=2.0)
    runner.stop()
    release.set()
    runner.wait(timeout=2.0)
    assert seen == []


def test_stop_during_factory_build_skips_run():
    """stop() пока фабрика ещё строит executor → после сборки run НЕ запускается."""
    factory_entered = threading.Event()
    release_factory = threading.Event()
    run_calls: list = []

    class _Ex:
        async def arun(self, messages, *, thread_id, **kw):
            run_calls.append(messages)
        def stop_run(self): pass
        def add_message(self, m): pass

    def factory():
        factory_entered.set()
        assert release_factory.wait(timeout=5.0)
        return _Ex()

    runner = AgentRunner(executor_factory=factory)
    assert runner.start(query="x", thread_id="t1") is True
    assert factory_entered.wait(timeout=2.0), "worker не дошёл до фабрики"
    runner.stop()  # executor ещё None — только флаг _stop_requested
    release_factory.set()
    runner.wait(timeout=2.0)
    assert runner._executor is not None  # сборка завершилась
    assert run_calls == []  # но run после stop() не запущен
    assert not runner.is_running()


def test_ensure_executor_build_once():
    from gc_tui.services.agent_runner import AgentRunner
    calls = {"n": 0}

    class _Ex:
        def stop_run(self): pass

    def factory():
        calls["n"] += 1
        return _Ex()

    r = AgentRunner(executor_factory=factory)
    r._ensure_executor()
    r._ensure_executor()
    assert calls["n"] == 1


def test_warm_builds_executor_in_background():
    import time
    from gc_tui.services.agent_runner import AgentRunner
    calls = {"n": 0}

    class _Ex:
        def stop_run(self): pass

    r = AgentRunner(executor_factory=lambda: (calls.__setitem__("n", calls["n"] + 1) or _Ex()))
    r.warm()
    for _ in range(100):
        if r._executor is not None:
            break
        time.sleep(0.02)
    assert r._executor is not None
    assert calls["n"] == 1


def test_warm_idempotent():
    import time
    from gc_tui.services.agent_runner import AgentRunner
    calls = {"n": 0}

    class _Ex:
        def stop_run(self): pass

    r = AgentRunner(executor_factory=lambda: (calls.__setitem__("n", calls["n"] + 1) or _Ex()))
    r.warm()
    for _ in range(100):
        if r._executor is not None:
            break
        time.sleep(0.02)
    r.warm()
    time.sleep(0.1)
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# confirm-канал (T1) — ConfirmChoice: once|session|deny
# ---------------------------------------------------------------------------

class _SimpleStub:
    """Минимальный stub-executor для тестов confirm-канала (без блокирующего run)."""
    async def arun(self, messages, *, thread_id, **kw): pass
    def stop_run(self): pass
    def add_message(self, m): pass


def test_confirm_submit_allow_remember_returns_allow_remember():
    """submit_confirm_reply('allow_remember') → make_tui_confirm-замыкание возвращает 'allow_remember'."""
    runner = AgentRunner(executor=_SimpleStub())

    def present(title, question, fields):
        def _reply():
            time.sleep(0.05)
            runner.submit_confirm_reply("allow_remember")
        threading.Thread(target=_reply, daemon=True).start()

    confirm = runner.make_tui_confirm(present)
    result = confirm(title="t", question="q", fields=[])
    assert result == "allow_remember"


def test_confirm_submit_allow_once_returns_allow_once():
    """submit_confirm_reply('allow_once') → make_tui_confirm-замыкание возвращает 'allow_once'."""
    runner = AgentRunner(executor=_SimpleStub())

    def present(title, question, fields):
        def _reply():
            time.sleep(0.05)
            runner.submit_confirm_reply("allow_once")
        threading.Thread(target=_reply, daemon=True).start()

    confirm = runner.make_tui_confirm(present)
    result = confirm(title="t", question="q", fields=[])
    assert result == "allow_once"


def test_confirm_submit_deny_remember_returns_deny_remember():
    """submit_confirm_reply('deny_remember') → make_tui_confirm-замыкание возвращает 'deny_remember'."""
    runner = AgentRunner(executor=_SimpleStub())

    def present(title, question, fields):
        def _reply():
            time.sleep(0.05)
            runner.submit_confirm_reply("deny_remember")
        threading.Thread(target=_reply, daemon=True).start()

    confirm = runner.make_tui_confirm(present)
    result = confirm(title="t", question="q", fields=[])
    assert result == "deny_remember"


def test_confirm_cancel_returns_deny_once():
    """cancel_confirm() → make_tui_confirm-замыкание возвращает 'deny_once'."""
    runner = AgentRunner(executor=_SimpleStub())

    def present(title, question, fields):
        threading.Thread(target=runner.cancel_confirm, daemon=True).start()

    confirm = runner.make_tui_confirm(present)
    result = confirm(title="t", question="q", fields=[])
    assert result == "deny_once"


def test_confirm_stop_unblocks_blocked_confirm():
    """stop() разблокирует агент-поток, зависший в confirm (возвращает 'deny_once')."""
    runner = AgentRunner(executor=_SimpleStub())

    # present ничего не делает — confirm будет ждать вечно, пока не придёт stop()
    def present(title, question, fields):
        pass

    confirm = runner.make_tui_confirm(present)
    result_holder: list[str] = []

    def _run_confirm():
        result_holder.append(confirm(title="t", question="q", fields=[]))

    t = threading.Thread(target=_run_confirm, daemon=True)
    t.start()
    time.sleep(0.05)  # дать confirm зависнуть
    runner.stop()
    t.join(timeout=2.0)
    assert not t.is_alive(), "confirm-поток не разблокировался за 2 с"
    assert result_holder == ["deny_once"]
