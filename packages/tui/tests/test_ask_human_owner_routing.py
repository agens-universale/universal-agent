import pytest


@pytest.mark.asyncio
async def test_owner_reset_on_unmount(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._ask_human_owner_runner = object()
        scr._unmount_ask_human_banner()
        assert scr._ask_human_owner_runner is None


@pytest.mark.asyncio
async def test_owner_reset_on_reset_render_state(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._ask_human_owner_runner = object()
        scr._reset_render_state()
        assert scr._ask_human_owner_runner is None


def _ask_human_rec():
    return {
        "payload": {
            "type": "function_call",
            "name": "turn_ask_human",
            "call_id": "c1",
            "arguments": '{"question": "Какой вопрос?"}',
            "id": "fc1",
        },
        "seq": 1,
        "ts": "2026-06-08T00:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_owner_set_on_live_render(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )

    class _Runner:
        def is_running(self):
            return True

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        runner = _Runner()
        app.active_runner = runner
        scr._render_records([_ask_human_rec()], live=True)
        await pilot.pause()
        assert scr._ask_human_owner_runner is runner


@pytest.mark.asyncio
async def test_owner_not_set_on_replay_render(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )

    class _Runner:
        def is_running(self):
            return True

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app.active_runner = _Runner()
        scr._render_records([_ask_human_rec()])  # live=False по умолчанию
        await pilot.pause()
        assert scr._ask_human_owner_runner is None


class _R:
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


@pytest.mark.asyncio
async def test_target_is_owner_when_live_and_active(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        r = _R(running=True)
        scr._ask_human_owner_runner = r
        app.active_runner = r
        assert scr._ask_human_target_runner() is r


@pytest.mark.asyncio
async def test_target_none_when_owner_differs(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._ask_human_owner_runner = _R(running=True)
        app.active_runner = _R(running=True)
        assert scr._ask_human_target_runner() is None


@pytest.mark.asyncio
async def test_target_none_when_owner_none(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._ask_human_owner_runner = None
        app.active_runner = _R(running=True)
        assert scr._ask_human_target_runner() is None


@pytest.mark.asyncio
async def test_target_none_when_not_running(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        r = _R(running=False)
        scr._ask_human_owner_runner = r
        app.active_runner = r
        assert scr._ask_human_target_runner() is None


class _Evt:
    attachments = ()

    def __init__(self, value):
        self.value = value


class _FakeRunner:
    def __init__(self, running=True):
        self._running = running
        self.submitted = []
        self.started = []
        self.stopped = False

    def is_running(self):
        return self._running

    def submit_ask_human_reply(self, text):
        self.submitted.append(text)

    def start(self, **kw):
        self.started.append(kw)
        return True

    def stop(self):
        self.stopped = True


def _set_ask_human_state(app):
    from gc_tui.state import AskHumanState

    app.tui_state.ask_human_state = AskHumanState(call_id="c1", question="Q?")


@pytest.mark.asyncio
async def test_live_owner_answer_goes_to_stdin(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        r = _FakeRunner(running=True)
        app.active_runner = r
        scr._ask_human_owner_runner = r
        _set_ask_human_state(app)
        await scr.on_command_text_area_submitted(_Evt("мой ответ"))
        await pilot.pause()
        assert r.submitted == ["мой ответ"]  # ушло владельцу в stdin
        assert r.started == []  # новый ход не стартовал


@pytest.mark.asyncio
async def test_replay_banner_answer_starts_new_turn(tmp_path):
    pytest.importorskip("textual")
    import uuid

    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Воспроизведённая плашка: владельца нет; в ячейке — мёртвый раннер.
        r = _FakeRunner(running=False)
        app.active_runner = r
        scr._ask_human_owner_runner = None
        tid = uuid.uuid4().hex
        app.tui_state.active_task_id = tid
        scr._active_task_id = tid
        scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
        _set_ask_human_state(app)
        await scr.on_command_text_area_submitted(_Evt("ответ новым ходом"))
        await pilot.pause()
        assert r.submitted == []  # в stdin не писали
        assert any(
            kw.get("query") == "ответ новым ходом" for kw in r.started
        )  # новый ход


@pytest.mark.asyncio
async def test_nonowner_running_is_stopped_then_new_turn(tmp_path):
    pytest.importorskip("textual")
    import uuid

    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", config={"model": "M"}
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        busy = _FakeRunner(running=True)  # чужой живой раннер в ячейке
        app.active_runner = busy
        scr._ask_human_owner_runner = _FakeRunner(
            running=True
        )  # владелец — другой
        fresh = _FakeRunner(running=False)
        # get_runner после _stop_and_replace_runner (active_runner=None) создаст
        # новый раннер из фабрики — подменим конструктор AgentRunner на возврат fresh.
        from gc_tui.services import agent_runner as _ar

        orig = _ar.AgentRunner
        _ar.AgentRunner = lambda *a, **k: fresh
        try:
            tid = uuid.uuid4().hex
            app.tui_state.active_task_id = tid
            scr._active_task_id = tid
            scr._store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
            _set_ask_human_state(app)
            await scr.on_command_text_area_submitted(_Evt("ответ"))
            await pilot.pause()
        finally:
            _ar.AgentRunner = orig
        assert busy.stopped is True  # чужой живой раннер остановлен
        assert busy.submitted == []  # ему в stdin не писали
        assert any(
            kw.get("query") == "ответ" for kw in fresh.started
        )  # новый ход
