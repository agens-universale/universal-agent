"""Скилл вживую — именем в реплике, ошибка строкой под ней (спека P3 §2.3).

Отдельной строки `[skill]` больше нет: имя скилла — первое слово реплики
(цвет `SKILL`), «не найден» — следствие `└ ✗ …` под подложкой.
"""

from gc_testkit.paths import TUI_PKG
from gc_testkit.tui_support import tui_settings
from gc_tui.services.stores.record_view import tui_section
import pytest
from rich.style import Style
from rich.text import Span

from gc_tui.palette import ERR, SKILL


def _as_coro(fn):
    """Обернуть синхронный двойник в корутину."""

    async def _wrapped(*a, **kw):
        return fn(*a, **kw)

    return _wrapped


def _patch_delivery(monkeypatch, *, available: bool) -> None:
    from gc_core.tools.skills import registry as sreg
    from gc_core.tools.skills import skill_delivery as sd

    monkeypatch.setattr(sreg, "get_skills_registry", lambda cfg: object())
    monkeypatch.setattr(
        sd, "build_skill_delivery", _as_coro(lambda skill_id, reg: "WIRE")
    )
    monkeypatch.setattr(
        sd, "skill_is_available", _as_coro(lambda skill_id, reg: available)
    )


class _Fake:
    async def arun(self, messages, **kw):
        import threading

        self._evt = threading.Event()
        self._evt.wait(timeout=2.0)

    def stop_run(self):
        if getattr(self, "_evt", None):
            self._evt.set()

    def add_message(self, m):
        pass


def _app(tmp_path):
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    return app


def _replies(scr):
    return list(scr.query("#trace UserReplyContainer"))


def _line(reply) -> str:
    line = reply._reply_lines(80)[0]
    return (line.prefix + line.body).plain


def _trace_texts(scr) -> str:
    return "\n".join(str(w.render()) for w in scr.query("#trace Static"))


def _wire_rec(content: str, display: str, skill: str | None) -> dict:
    payload = {"role": "user", "content": content, "display_content": display}
    if skill:
        payload["skill_loaded"] = skill
    return {"payload": payload, "seq": 1, "ts": "2026-06-26T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_slash_skill_name_is_one_reply_with_skill_name(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=True)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        sid = "supercoder:brainstorming"
        scr._skills_cache = [{"id": sid}]
        await scr._handle_command("brainstorming придумай")
        await pilot.pause(0.3)
        # Опрос записи треда: реплика сверяется, дубля нет.
        scr._render_records(
            [_wire_rec("WIRE\nпридумай", "придумай", sid)], live=True
        )
        await pilot.pause()
        replies = _replies(scr)
        assert [_line(r) for r in replies] == [
            "› /supercoder:brainstorming придумай"
        ]
        assert replies[0]._reply_lines(80)[0].body.spans == [
            Span(0, len("/supercoder:brainstorming"), Style(color=SKILL))
        ]
        assert replies[0].plain_text == "/supercoder:brainstorming придумай"
        assert replies[0]._consequences == []
        assert "загружен" not in _trace_texts(scr)


@pytest.mark.asyncio
async def test_missing_skill_is_consequence_under_reply(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=False)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "brainstorm"}]
        await scr._handle_command("skill brainstorm придумай")
        await pilot.pause(0.3)
        (reply,) = _replies(scr)
        assert _line(reply) == "› /brainstorm придумай"
        # Имя ненайденного скилла — цветом текста, не `SKILL`.
        assert reply._reply_lines(80)[0].body.spans == []
        (cons,) = reply._consequences
        assert cons.plain == (
            "скилл brainstorm не найден — отправлено без него"
        )
        assert cons.style == ERR


@pytest.mark.asyncio
async def test_mid_run_skill_only_is_one_reply(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=True)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "pack:x"}]
        await scr._handle_command("skill x начни")
        await pilot.pause(0.2)
        assert pilot.app.active_runner.is_running()
        await scr._handle_command("skill x")
        await pilot.pause()
        assert [_line(r) for r in _replies(scr)] == [
            "› /pack:x начни",
            "› /pack:x",
        ]
        # Опрос записей треда: обе реплики сверяются, дублей нет.
        scr._render_records(
            [
                _wire_rec("WIRE\nначни", "начни", "pack:x"),
                _wire_rec("WIRE", "", "pack:x"),
            ],
            live=True,
        )
        await pilot.pause()
        assert [_line(r) for r in _replies(scr)] == [
            "› /pack:x начни",
            "› /pack:x",
        ]
        assert scr._pending_user_widgets == {}


@pytest.mark.asyncio
async def test_idle_skill_without_text_answers_command(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=True)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "pack:x"}]
        await scr._handle_command("skill x")
        await pilot.pause()
        from gc_tui.widgets.trace_row import TraceRow

        rows = [(r._prefix, str(r._content)) for r in scr.query(TraceRow)]
        assert ("· ", "скилл x применится к следующему сообщению") in rows
        assert "[skill]" not in _trace_texts(scr)


def test_no_skill_marker_rows_in_sources():
    hits = [
        str(p.relative_to(TUI_PKG))
        for p in TUI_PKG.rglob("*")
        if p.suffix in {".py", ".tcss"}
        and any(
            mark in p.read_text(encoding="utf-8")
            for mark in ("[skill]", "skill-marker")
        )
    ]
    assert hits == []


# --- неизвестный скилл: агенту не уходит ничего лишнего ---------------------

_TID = "s" * 32


class _Runner:
    """Раннер-двойник: идёт ли ход, что ушло в очередь, что стартовало."""

    def __init__(self, running: bool):
        self.running = running
        self.added: list[dict] = []
        self.starts: list[dict] = []

    def is_running(self) -> bool:
        return self.running

    def is_child_input_active(self) -> bool:
        return False

    def add_message(self, message: dict) -> None:
        self.added.append(message)

    def start(self, **kw) -> bool:
        self.starts.append(kw)
        return True


def _activate(app, scr, threads) -> None:
    import json

    from gc_tui.services.stores.file_store import FileTaskStore

    threads.mkdir(exist_ok=True)
    (threads / f"data_cli_{_TID}.jsonl").write_text("", encoding="utf-8")
    meta = {"thread_id": _TID, "messages_count": 0, "tui": {}}
    (threads / f"meta_cli_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    scr._active_task_id = _TID
    app.tui_state.active_task_id = _TID
    scr._store = FileTaskStore(threads_dir=str(threads), system="cli")


def _meta_skills(scr) -> list:
    return tui_section(scr._store.get_record(_TID)).get("skills", [])


async def _unknown_skill_session(pilot, tmp_path, *, running: bool):
    scr = pilot.app.screen
    _activate(pilot.app, scr, tmp_path / "threads")
    scr._skills_cache = [{"id": "other"}]
    runner = _Runner(running)
    pilot.app.active_runner = runner
    return scr, runner


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_unknown_skill_without_text_sends_nothing(
    tmp_path, monkeypatch, running
):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=False)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _unknown_skill_session(
            pilot, tmp_path, running=running
        )
        await scr._handle_command("skill nope")
        await pilot.pause()
        assert runner.added == [] and runner.starts == []
        assert pilot.app.tui_state.pending_skill_id is None
        assert _replies(scr) == []
        assert "[!] скилл не найден: /nope" in _trace_texts(scr)
        assert _meta_skills(scr) == []


@pytest.mark.asyncio
async def test_unknown_skill_with_text_idle_sends_text_without_it(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=False)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _unknown_skill_session(
            pilot, tmp_path, running=False
        )
        await scr._handle_command("skill nope почини тест")
        await pilot.pause()
        (start,) = runner.starts
        assert start["query"] == "почини тест"
        assert start["skill_loaded"] is None
        (reply,) = _replies(scr)
        assert reply.state == "pending"
        assert _line(reply) == "› /nope почини тест"
        assert [c.plain for c in reply._consequences] == [
            "скилл nope не найден — отправлено без него"
        ]
        assert _meta_skills(scr) == []


@pytest.mark.asyncio
async def test_unknown_skill_with_text_mid_run_queues_text_without_it(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=False)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _unknown_skill_session(
            pilot, tmp_path, running=True
        )
        await scr._handle_command("skill nope почини тест")
        await pilot.pause()
        (message,) = runner.added
        assert message["content"] == "почини тест"
        assert "skill_loaded" not in message
        (reply,) = _replies(scr)
        assert reply.state == "queued"
        assert _line(reply) == "› /nope почини тест"
        assert [c.plain for c in reply._consequences] == [
            "скилл nope не найден — отправлено без него"
        ]
        assert _meta_skills(scr) == []


# --- реестр скиллов не читает диск в потоке интерфейса -----------------------


class _BlockingRegistry:
    """Реестр, который, как настоящий, обходит диск синхронно внутри
    async-метода: на цикле интерфейса он держал бы ввод."""

    async def load_skill_detail(self, skill_id):
        import time

        time.sleep(0.05)
        if skill_id != "pack:x":
            raise KeyError(skill_id)
        return {"instructions": "BODY"}


def _sent(runner) -> list[str]:
    return [m["content"] for m in runner.added] + [
        s["query"] for s in runner.starts
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
@pytest.mark.parametrize(
    "command, body_sent",
    [
        ("skill nope", False),
        ("skill nope почини", False),
        ("skill x", True),
        ("skill x почини", True),
    ],
)
async def test_skill_command_never_scans_registry_on_ui_loop(
    tmp_path, monkeypatch, running, command, body_sent
):
    """Без заглушки «скилл загружаем»: есть ли скилл — по прогретому кэшу,
    тело доставки — реестром вне цикла интерфейса."""
    pytest.importorskip("textual")
    import threading

    from gc_core.tools.skills import registry as sreg

    built_on_loop: list[bool] = []

    def _registry(cfg):
        main = threading.current_thread() is threading.main_thread()
        built_on_loop.append(main)
        return _BlockingRegistry()

    monkeypatch.setattr(sreg, "get_skills_registry", _registry)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _unknown_skill_session(
            pilot, tmp_path, running=running
        )
        scr._skills_cache = [{"id": "pack:x"}]
        await scr._handle_command(command)
        await pilot.pause()
        sent = _sent(runner)
    # Уходит текст; `/skill x` без текста — только во время хода (при
    # простое он ждёт следующего сообщения).
    with_text = "почини" in command
    assert True not in built_on_loop
    assert len(sent) == int(with_text or (running and body_sent))
    assert any("BODY" in s for s in sent) == (body_sent and bool(sent))
    assert any("почини" in s for s in sent) == with_text


# --- отправки по одному: порядок = порядок набора ---------------------------


class _SlowRegistry:
    """Реестр, отвечающий медленно: отправка ждёт тело доставки вне цикла."""

    async def load_skill_detail(self, skill_id):
        import time

        time.sleep(0.3)
        return {"instructions": "BODY"}


class _StartingRunner(_Runner):
    """После `start` ход идёт — следующие сообщения уходят в очередь."""

    def start(self, **kw) -> bool:
        self.running = True
        return super().start(**kw)


def _slow_registry(monkeypatch) -> None:
    from gc_core.tools.skills import registry as sreg

    monkeypatch.setattr(
        sreg, "get_skills_registry", lambda cfg: _SlowRegistry()
    )


async def _submit_back_to_back(pilot, *texts: str) -> None:
    """Enter подряд: сообщения Submitted ставятся в очередь поля ввода,
    как при быстром наборе, — без ожидания обработки первого."""
    from gc_tui.widgets.command_input import CommandTextArea

    inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
    for text in texts:
        inp.post_message(CommandTextArea.Submitted(text))


async def _until(pilot, cond) -> None:
    for _ in range(100):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("не дождались")


@pytest.mark.asyncio
async def test_fast_enter_with_pending_skill_starts_once_in_order(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    _slow_registry(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, tmp_path / "threads")
        scr._skills_cache = [{"id": "pack:x"}]
        runner = _StartingRunner(running=False)
        pilot.app.active_runner = runner
        pilot.app.tui_state.pending_skill_id = "pack:x"
        await _submit_back_to_back(pilot, "первое", "второе")
        await _until(pilot, lambda: runner.starts and runner.added)
        await pilot.pause(0.4)
        (start,) = runner.starts
        assert start["query"].endswith("первое") and "BODY" in start["query"]
        assert [m["content"] for m in runner.added] == ["второе"]
        assert [r._text for r in _replies(scr)] == ["первое", "второе"]
        assert [r.state for r in _replies(scr)] == ["pending", "queued"]


@pytest.mark.asyncio
async def test_fast_enter_skill_with_text_then_plain_keeps_order(
    tmp_path, monkeypatch
):
    pytest.importorskip("textual")
    _slow_registry(monkeypatch)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, tmp_path / "threads")
        scr._skills_cache = [{"id": "pack:x"}]
        runner = _Runner(running=True)
        pilot.app.active_runner = runner
        await _submit_back_to_back(pilot, "/skill x почини", "потом это")
        await _until(pilot, lambda: len(runner.added) == 2)
        first, second = runner.added
        assert "BODY" in first["content"] and "почини" in first["content"]
        assert second["content"] == "потом это"
        assert [r._text for r in _replies(scr)] == ["почини", "потом это"]


@pytest.mark.asyncio
async def test_run_ends_while_skill_loads_stages_it_for_next_message(
    tmp_path, monkeypatch
):
    """Ход кончился, пока тело скилла читалось вне цикла: `/skill x` не
    уходит в очередь остановленного раннера, а ждёт следующего сообщения."""
    pytest.importorskip("textual")
    from gc_core.tools.skills import registry as sreg

    runner = _Runner(running=True)

    class _EndingRegistry:
        async def load_skill_detail(self, skill_id):
            runner.running = False
            return {"instructions": "BODY"}

    monkeypatch.setattr(
        sreg, "get_skills_registry", lambda cfg: _EndingRegistry()
    )
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        _activate(pilot.app, scr, tmp_path / "threads")
        scr._skills_cache = [{"id": "pack:x"}]
        pilot.app.active_runner = runner
        await scr._handle_command("skill x")
        await pilot.pause()
        assert runner.added == []
        assert pilot.app.tui_state.pending_skill_id == "pack:x"
        assert _replies(scr) == []


@pytest.mark.asyncio
async def test_skill_command_takes_skill_name_in_other_layout(
    tmp_path, monkeypatch
):
    """`/skill икфштыещкьштп …` — имя скилла в чужой раскладке."""
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=True)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "supercoder:brainstorming"}]
        await scr._handle_command("skill икфштыещкьштп придумай")
        await pilot.pause(0.3)
        assert [_line(r) for r in _replies(scr)] == [
            "› /supercoder:brainstorming придумай"
        ]


@pytest.mark.asyncio
async def test_skill_name_is_translated_by_skills_only(tmp_path, monkeypatch):
    """`ьщвуд` — это `model` в чужой раскладке, но `/skill` ищет скилл:
    ошибка называет набранное, а не команду."""
    pytest.importorskip("textual")
    _patch_delivery(monkeypatch, available=True)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "supercoder:brainstorming"}]
        await scr._handle_command("skill ьщвуд")
        await pilot.pause(0.2)
        texts = _trace_texts(scr)
        assert "ьщвуд" in texts
        assert "model" not in texts
