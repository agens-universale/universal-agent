"""Скилл вживую — именем в реплике, ошибка строкой под ней (спека P3 §2.3).

Отдельной строки `[skill]` больше нет: имя скилла — первое слово реплики
(цвет `SKILL`), «не найден» — следствие `└ ✗ …` под подложкой.
"""

from gc_testkit.paths import TUI_PKG
from gc_testkit.tui_support import tui_settings
import pytest
from rich.style import Style

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
        assert [_line(r) for r in replies] == ["› /brainstorming придумай"]
        span = replies[0]._reply_lines(80)[0].body.spans[0]
        assert (span.start, span.end) == (0, len("/brainstorming"))
        assert span.style == Style(color=SKILL)
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
            "✗ скилл brainstorm не найден — отправлено без него"
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
            "› /x начни",
            "› /x",
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
        assert [_line(r) for r in _replies(scr)] == ["› /x начни", "› /x"]
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
