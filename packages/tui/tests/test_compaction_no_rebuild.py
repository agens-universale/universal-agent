"""Шаг сжатия дописывается в трейс, а не перерисовывает его (спека 3.2, 3.3).

Тред пишется настоящими функциями gc_core.thread_store; сжатие — шагами ядра
по одному (1 — data в архив, 2 — meta в архив, 3 — новый data со сводкой,
4 — новая meta с эпохой, 5 — дописка сохранённых сообщений) с прямым тиком
опроса между ними. Эталон «с нуля» — повторный attach в том же приложении.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from gc_core.attachments.model import (
    ORIGIN_USER,
    Attachment,
    chip_text,
    user_label,
)
from gc_core.message_policy.user_message_policy import (
    TECHNICAL_TYPE_DIALOG_SUMMARY,
    wrap_technical_user_content,
)
from gc_core.thread_store import (
    append_new_thread_messages,
    append_subagent_trace_record,
    archive_and_replace_thread_files,
    delete_thread,
    load_thread_meta,
    reset_thread_files,
    save_thread_meta,
)
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.screens.main import TraceScreen
from gc_tui.services.stores.file_store import FileTaskStore
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.user_reply import UserReplyContainer

_SYS = "cli"
_TID = "cmp1"
_MARK = "контекст сжат"


# ─── тред на диске ───────────────────────────────────────────────────────────


def _user(mid: str, text: str, **extra) -> dict:
    return {"id": mid, "role": "user", "content": text, **extra}


def _agent(mid: str, text: str) -> dict:
    return {"id": mid, "role": "assistant", "content": text}


def _call(mid: str, cid: str, name: str = "fs_read_file") -> dict:
    return {
        "id": mid,
        "type": "function_call",
        "name": name,
        "call_id": cid,
        "arguments": json.dumps({"path": f"{cid}.py"}),
    }


def _out(mid: str, cid: str, output: str = "ok") -> dict:
    return {
        "id": mid,
        "type": "function_call_output",
        "call_id": cid,
        "output": output,
    }


def _ask(mid: str, cid: str) -> dict:
    return {
        "id": mid,
        "type": "function_call",
        "name": "ask_human",
        "call_id": cid,
        "arguments": json.dumps({"question": "Подтверждаете?"}),
    }


def _summary(mid: str, text: str = "кратко") -> dict:
    return {
        "id": mid,
        "role": "user",
        "content": wrap_technical_user_content(
            f"Summarized context from previous dialogue.\n{text}",
            technical_type=TECHNICAL_TYPE_DIALOG_SUMMARY,
        ),
    }


def _image(tmp_path: Path, n: int) -> Attachment:
    p = tmp_path / f"img{n}.png"
    p.write_bytes(b"\x89PNG")
    return Attachment(
        id=f"{n:016x}",
        kind="image",
        mime="image/png",
        width=10,
        height=10,
        size=4,
        path=str(p),
        origin=ORIGIN_USER,
        label=user_label(n),
        source="clipboard",
        orig_width=10,
        orig_height=10,
    )


class _Thread:
    """Тред на диске: запись сообщений и шаги сжатия ядра по одному."""

    def __init__(self, tmp_path: Path) -> None:
        self.dir = str(tmp_path)
        self.seq = 0
        self.meta: dict = {}
        save_thread_meta(self.dir, _SYS, _TID, {})

    def _paths(self) -> tuple[str, str]:
        return (
            os.path.join(self.dir, f"data_{_SYS}_{_TID}.jsonl"),
            os.path.join(self.dir, f"meta_{_SYS}_{_TID}.json"),
        )

    def append(self, *msgs: dict) -> None:
        append_new_thread_messages(
            self.dir, _SYS, _TID, list(msgs), set(), start_seq=self.seq
        )
        self.seq += len(msgs)

    def steps(self, suffix: str, *preserved: dict) -> list:
        """Шаги ядра 1–5 как вызовы (5 — дописка сохранённых сообщений)."""

        def step1() -> None:
            self.meta = load_thread_meta(self.dir, _SYS, _TID)
            data, _ = self._paths()
            os.replace(
                data,
                data.replace(".jsonl", f"__pre_summary__{suffix}.jsonl"),
            )

        def step2() -> None:
            _, meta = self._paths()
            os.replace(
                meta, meta.replace(".json", f"__pre_summary__{suffix}.json")
            )

        def step3() -> None:
            archive_and_replace_thread_files(
                self.dir,
                _SYS,
                _TID,
                [_summary(f"sum_{suffix}", f"сводка {suffix}")],
                archive_suffix=suffix,
            )
            self.seq = 1

        def step4() -> None:
            meta = {**self.meta, "last_summary_archive_suffix": suffix}
            save_thread_meta(self.dir, _SYS, _TID, meta)

        def step5() -> None:
            if preserved:
                self.append(*preserved)

        return [step1, step2, step3, step4, step5]


def _compact_ticking(thread: _Thread, scr, suffix: str, *preserved) -> None:
    """Сжатие на ходу: тик опроса после каждого шага ядра."""
    for step in thread.steps(suffix, *preserved):
        step()
        scr._poll_active()


def _compact_silently(thread: _Thread, suffix: str, *preserved) -> None:
    for step in thread.steps(suffix, *preserved):
        step()


# ─── экран ───────────────────────────────────────────────────────────────────


def _trace_lines(app) -> list[str]:
    """Тексты строк трейса по порядку (контейнеры без текста пропускаются)."""
    trace = app.screen.query_one("#trace")
    lines = []
    for c in trace.walk_children():
        r = c.render()
        plain = r if isinstance(r, str) else getattr(r, "plain", None)
        if isinstance(plain, str):
            lines.append(plain)
    return lines


def _trace_text(app) -> str:
    return "\n".join(_trace_lines(app))


def _events(scr) -> list[tuple]:
    return [(e.no, e.kind, e.summary) for e in scr._task_events]


def _count(monkeypatch, scr, name: str) -> list:
    """Счётчик вызовов метода экрана; оригинал продолжает работать."""
    calls: list = []
    orig = getattr(scr, name)

    def wrapper(*a, **kw):
        calls.append(a)
        return orig(*a, **kw)

    monkeypatch.setattr(scr, name, wrapper)
    return calls


class _LiveRunner:
    """Живой агент-заглушка: ждёт ответа, подтверждение не отменяется."""

    def __init__(self) -> None:
        self.cancel_confirm_calls = 0

    def is_running(self) -> bool:
        return True

    def submit_ask_human_reply(self, text: str) -> None:
        pass

    def cancel_ask_human(self) -> None:
        pass

    def cancel_confirm(self) -> None:
        self.cancel_confirm_calls += 1


@asynccontextmanager
async def _session(tmp_path: Path, *, runner=None, executor=None, size=None):
    app = TuiApp(
        threads_dir=str(tmp_path),
        system=_SYS,
        settings=tui_settings({"agent.model": "M"}),
    )
    if executor is not None:
        app._executor_factory = lambda: executor
    if runner is not None:
        app.active_runner = runner
    kw = {"size": size} if size else {}
    async with app.run_test(**kw) as pilot:
        scr = app.screen
        scr.attach_active_task(_TID)
        await pilot.pause(0.05)
        yield app, pilot, scr


async def _fresh(pilot, scr) -> tuple[list, list]:
    """Эталон: повторный attach того же треда с нуля."""
    scr.attach_active_task(_TID)
    await pilot.pause(0.05)
    return _events(scr), _trace_lines(pilot.app)


async def _wait_until(pilot, cond, what: str) -> None:
    for _ in range(200):
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError(f"timeout: {what}")


def _read_pending(tmp_path: Path) -> list:
    p = tmp_path / f"meta_{_SYS}_{_TID}.json"
    if not p.exists():
        return []
    meta = json.loads(p.read_text(encoding="utf-8"))
    return (meta.get("tui") or {}).get("pending_user_messages", [])


def _base_thread(tmp_path: Path) -> _Thread:
    thread = _Thread(tmp_path)
    thread.append(
        _user(
            "u1",
            f"{chip_text(1)} посмотри картинку",
            attachments=[_image(tmp_path, 1).to_dict()],
        ),
        _agent("a1", "Смотрю."),
        _call("fc1", "c1"),
        _out("fo1", "c1", "первый вывод"),
    )
    return thread


# ─── эквивалентность и отсутствие перемонтажа ────────────────────────────────


@pytest.mark.asyncio
async def test_compaction_equivalent_to_fresh_attach(tmp_path, monkeypatch):
    thread = _base_thread(tmp_path)
    # Агент жив: идущий вызов без вывода не финализируется «прервано».
    async with _session(tmp_path, runner=_LiveRunner()) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        # Хвост архива: дописан после курсора, тиком ещё не прочитан.
        thread.append(_user("u2", "дальше"), _call("fc2", "c2"))
        _compact_ticking(
            thread,
            scr,
            "S1",
            _user("u2", "дальше"),
            _call("fc2", "c2"),
            _out("fo2", "c2", "второй вывод"),
        )
        thread.append(_agent("a2", "Готово."))
        scr._poll_active()
        await pilot.pause(0.05)
        live, live_text = _events(scr), _trace_lines(app)

        fresh, fresh_text = await _fresh(pilot, scr)

    assert rebuilds == []
    assert live == fresh
    assert live_text == fresh_text
    assert "\n".join(live_text).count(_MARK) == 1


@pytest.mark.asyncio
async def test_compaction_keeps_existing_widgets(tmp_path, monkeypatch):
    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        trace = app.screen.query_one("#trace")
        before = list(trace.children)
        clear_calls = _count(monkeypatch, scr, "_clear_trace")
        segments_calls = _count(monkeypatch, scr, "_render_segments")
        thread.append(_user("u2", "дальше"))
        _compact_ticking(thread, scr, "S1", _user("u2", "дальше"))
        await pilot.pause(0.05)

        assert all(w in trace.children for w in before)
        assert len(trace.children) > len(before)
        assert clear_calls == [] and segments_calls == []


@pytest.mark.asyncio
async def test_compaction_steps_one_by_one_single_marker(
    tmp_path, monkeypatch
):
    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        thread.append(_user("u2", "дальше"))
        _compact_ticking(thread, scr, "S1", _user("u2", "дальше"))
        thread.append(_agent("a2", "между сжатиями"))
        scr._poll_active()
        _compact_ticking(thread, scr, "S2", _agent("a2", "между сжатиями"))
        await pilot.pause(0.05)
        text = _trace_text(app)

    assert rebuilds == []
    assert text.count(f"{_MARK} #1") == 1
    assert text.count(f"{_MARK} #2") == 1
    assert text.count(_MARK) == 2
    assert text.count("дальше") == 1
    assert text.count("между сжатиями") == 1


@pytest.mark.asyncio
async def test_two_compactions_back_to_back(tmp_path, monkeypatch):
    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        _compact_silently(thread, "S1")
        scr._poll_active()
        _compact_silently(thread, "S2")
        scr._poll_active()
        await pilot.pause(0.05)
        live, live_text = _events(scr), _trace_lines(app)

        fresh, fresh_text = await _fresh(pilot, scr)

    assert rebuilds == []
    assert live == fresh
    assert live_text == fresh_text
    assert "\n".join(live_text).count(_MARK) == 2


def _row_for(scr, call_id: str) -> list[ToolCallRow]:
    return [w for w in scr.query(ToolCallRow) if w.call_id == call_id]


@pytest.mark.asyncio
async def test_call_in_archive_tail_gets_output_from_live(tmp_path):
    thread = _base_thread(tmp_path)
    # Агент жив: идущий вызов без вывода не финализируется «прервано».
    async with _session(tmp_path, runner=_LiveRunner()) as (app, pilot, scr):
        thread.append(_call("fc2", "c2"))
        _compact_ticking(thread, scr, "S1", _call("fc2", "c2"))
        assert len(_row_for(scr, "c2")) == 1
        thread.append(_out("fo2", "c2", "второй вывод"))
        scr._poll_active()
        await pilot.pause(0.05)
        rows = _row_for(scr, "c2")
        text = _trace_text(app)

    assert len(rows) == 1
    assert rows[0]._result is not None
    assert text.count("второй вывод") == 1


@pytest.mark.asyncio
async def test_result_in_live_records_same_step(tmp_path):
    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        thread.append(_call("fc2", "c2"))
        _compact_silently(
            thread, "S1", _call("fc2", "c2"), _out("fo2", "c2", "второй")
        )
        scr._poll_active()
        await pilot.pause(0.05)
        rows = _row_for(scr, "c2")
        live, live_text = _events(scr), _trace_lines(app)

        fresh, fresh_text = await _fresh(pilot, scr)

    assert len(rows) == 1
    assert rows[0]._result is not None
    assert live == fresh
    assert live_text == fresh_text


@pytest.mark.asyncio
async def test_apply_failure_falls_back_to_rebuild(tmp_path, monkeypatch):
    thread = _base_thread(tmp_path)
    orig = TraceScreen._mount_compaction_marker
    raised: list = []

    def boom_once(self, step):
        if not raised:
            raised.append(step)
            raise RuntimeError("boom")
        return orig(self, step)

    monkeypatch.setattr(TraceScreen, "_mount_compaction_marker", boom_once)
    # Агент жив: идущий вызов без вывода не финализируется «прервано».
    async with _session(tmp_path, runner=_LiveRunner()) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        thread.append(_user("u2", "дальше"), _call("fc2", "c2"))
        _compact_ticking(
            thread, scr, "S1", _call("fc2", "c2"), _out("fo2", "c2", "вывод")
        )
        await pilot.pause(0.05)
        live, live_text = _events(scr), _trace_lines(app)

        fresh, fresh_text = await _fresh(pilot, scr)

    assert len(raised) == 1
    assert len(rebuilds) == 1
    assert live == fresh
    assert live_text == fresh_text


@pytest.mark.parametrize("drop", [reset_thread_files, delete_thread])
@pytest.mark.asyncio
async def test_thread_reset_or_deleted_rebuilds(tmp_path, monkeypatch, drop):
    thread = _base_thread(tmp_path)
    _compact_silently(thread, "S1")
    async with _session(tmp_path) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        drop(thread.dir, _SYS, _TID)
        for _ in range(3):
            scr._poll_active()
        await pilot.pause(0.05)

    assert len(rebuilds) == 1


@pytest.mark.asyncio
async def test_bounded_wait_rebuilds_once_then_plain_ticks(
    tmp_path, monkeypatch
):
    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        for step in thread.steps("S1")[:3]:
            step()
            scr._poll_active()
        for _ in range(7):
            scr._poll_active()
        assert rebuilds == []
        scr._poll_active()
        assert len(rebuilds) == 1
        for _ in range(10):
            scr._poll_active()
        await pilot.pause(0.05)

        assert len(rebuilds) == 1
        assert scr._cursor.wait_ticks == 0


@pytest.mark.asyncio
async def test_stream_block_after_compaction_like_plain_tick(tmp_path):
    from gc_tui.widgets.stream_block import StreamBlock

    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        store = FileTaskStore(thread.dir, system=_SYS)

        # Обычная порция: запись снимает стрим-блок.
        scr._stream_block = StreamBlock()
        scr._trace_mount(scr._stream_block)
        plain = [_user("u2", "обычная порция")]
        thread.append(*plain)
        tail = store.tail_events(_TID, scr._cursor)
        scr._cursor = tail.next_cursor
        scr._render_records(tail.new_records, live=True)
        plain_state = scr._stream_block

        # Шаг сжатия того же состава.
        block = StreamBlock()
        scr._stream_block = block
        scr._trace_mount(block)
        thread.append(_user("u3", "в хвост архива"))
        _compact_silently(thread, "S1")
        tail = store.tail_events(_TID, scr._cursor)
        assert tail.compaction is not None
        scr._apply_compaction(tail.compaction, tail.next_cursor)
        await pilot.pause(0.05)

        assert plain_state is None
        assert scr._stream_block is plain_state
        assert block not in app.screen.query_one("#trace").children


@pytest.mark.asyncio
async def test_compaction_under_selection_applied_after_release(
    tmp_path, monkeypatch
):
    thread = _base_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        rebuilds = _count(monkeypatch, scr, "_rebuild_from_store")
        frozen = [True]
        monkeypatch.setattr(scr, "_ui_frozen_for_select", lambda: frozen[0])
        thread.append(_user("u2", "хвост архива"))
        _compact_ticking(thread, scr, "S1", _user("u2", "хвост архива"))
        scr._poll_active()
        frozen[0] = False
        scr._poll_active()
        await pilot.pause(0.05)
        text = _trace_text(app)

    assert rebuilds == []
    assert text.count(_MARK) == 1
    assert text.count("хвост архива") == 1


# ─── побочные действия (спека 3.3) ───────────────────────────────────────────


def _ask_thread(tmp_path: Path) -> _Thread:
    thread = _Thread(tmp_path)
    thread.append(_user("u1", "запусти"), _ask("ah1", "call_ah"))
    return thread


@pytest.mark.asyncio
async def test_answered_ask_human_banner_absent_after_compaction(tmp_path):
    thread = _ask_thread(tmp_path)
    async with _session(tmp_path, runner=_LiveRunner()) as (app, pilot, scr):
        assert scr._ask_human_banner is not None
        # Продолжение диалога — в том же шаге (новый живой файл).
        _compact_silently(thread, "S1", _agent("a1", "Продолжаю."))
        scr._poll_active()
        await pilot.pause(0.05)

        assert scr._ask_human_banner is None
        assert app.tui_state.ask_human_state is None
        assert "прервано" not in _trace_text(app)


@pytest.mark.asyncio
async def test_waiting_ask_human_banner_survives_compaction(tmp_path):
    thread = _ask_thread(tmp_path)
    async with _session(tmp_path, runner=_LiveRunner()) as (app, pilot, scr):
        banner = scr._ask_human_banner
        assert banner is not None
        _compact_ticking(thread, scr, "S1", _ask("ah1", "call_ah"))
        await pilot.pause(0.05)

        assert scr._ask_human_banner is banner
        assert _trace_text(app).count(_MARK) == 1


class _BlockingExecutor:
    """Исполнитель-заглушка: блокируется до stop_run, в тред не пишет."""

    def __init__(self) -> None:
        self._evt = threading.Event()

    async def arun(self, messages, *, thread_id, **kw):
        self._evt.wait(timeout=30.0)

    def stop_run(self) -> None:
        self._evt.set()

    def add_message(self, m) -> None:
        pass


async def _send(pilot, tmp_path: Path, text: str) -> UserReplyContainer:
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.press("enter")
    await _wait_until(
        pilot,
        lambda: any(r["content"] == text for r in _read_pending(tmp_path)),
        f"pending-запись {text}",
    )
    widgets = [
        w for w in pilot.app.query(UserReplyContainer) if w._text == text
    ]
    assert len(widgets) == 1 and widgets[0].state == "pending"
    return widgets[0]


@pytest.mark.parametrize("where", ["archive_tail", "live_only"])
@pytest.mark.asyncio
async def test_pending_delivered_during_compaction_becomes_normal(
    tmp_path, where
):
    thread = _base_thread(tmp_path)
    fake = _BlockingExecutor()
    async with _session(tmp_path, executor=fake) as (app, pilot, scr):
        widget = await _send(pilot, tmp_path, "доставлено")
        delivered = _user("ud", "доставлено")
        if where == "archive_tail":
            thread.append(delivered)
            _compact_ticking(thread, scr, "S1", delivered)
        else:
            _compact_ticking(thread, scr, "S1", delivered)
        await pilot.pause(0.05)
        same = [
            w for w in app.query(UserReplyContainer) if w._text == "доставлено"
        ]
        fake.stop_run()

    assert same == [widget]
    assert widget.state == "normal"
    assert _read_pending(tmp_path) == []


@pytest.mark.parametrize("when", ["mid_turn", "turn_end"])
@pytest.mark.asyncio
async def test_queued_pending_stays_pending_after_compaction(tmp_path, when):
    thread = _base_thread(tmp_path)
    fake = _BlockingExecutor()
    async with _session(tmp_path, executor=fake) as (app, pilot, scr):
        first = await _send(pilot, tmp_path, "первое")
        thread.append(_user("uf", "первое"))
        scr._poll_active()
        await _wait_until(
            pilot, lambda: first.state == "normal", "первое доставлено"
        )
        widget = await _send(pilot, tmp_path, "в очереди")
        if when == "mid_turn":
            thread.append(_call("fc2", "c2"), _out("fo2", "c2", "вывод"))
        else:
            thread.append(_agent("af", "Ответ хода."))
        _compact_ticking(thread, scr, "S1")
        await pilot.pause(0.05)
        failed = [
            w for w in app.query(UserReplyContainer) if w.state == "failed"
        ]
        queued = [
            w for w in app.query(UserReplyContainer) if w._text == "в очереди"
        ]
        pending = _read_pending(tmp_path)
        fake.stop_run()

    assert queued == [widget]
    assert widget.state == "pending"
    assert [r["status"] for r in pending] == ["pending"]
    assert failed == []


@pytest.mark.asyncio
async def test_thinking_indicator_survives_mid_turn_compaction(tmp_path):
    thread = _base_thread(tmp_path)
    fake = _BlockingExecutor()
    async with _session(tmp_path, executor=fake) as (app, pilot, scr):
        await _send(pilot, tmp_path, "работай")
        await _wait_until(
            pilot, lambda: scr._thinking is not None, "индикатор"
        )
        indicator = scr._thinking
        thread.append(_user("uw", "работай"), _call("fc2", "c2"))
        _compact_ticking(thread, scr, "S1", _out("fo2", "c2", "вывод"))
        await pilot.pause(0.05)
        after = scr._thinking
        fake.stop_run()

    assert after is indicator


@pytest.mark.asyncio
async def test_confirm_banner_not_cancelled_by_compaction(tmp_path):
    thread = _base_thread(tmp_path)
    runner = _LiveRunner()
    async with _session(tmp_path, runner=runner) as (app, pilot, scr):
        scr.show_confirm_banner("Действие", "Разрешить?")
        await pilot.pause(0.05)
        banner = scr._confirm_banner
        assert banner is not None
        runner.cancel_confirm_calls = 0  # attach отменяет всегда — не в счёт
        _compact_ticking(thread, scr, "S1")
        await pilot.pause(0.05)

        assert runner.cancel_confirm_calls == 0
        assert scr._confirm_banner is banner
        assert scr._confirm_active


@pytest.mark.asyncio
async def test_scroll_position_kept_after_compaction(tmp_path):
    thread = _Thread(tmp_path)
    for i in range(30):
        thread.append(_user(f"u{i}", f"реплика {i}"))
    async with _session(tmp_path, size=(80, 24)) as (app, pilot, scr):
        trace = app.screen.query_one("#trace")
        assert trace.max_scroll_y > 5
        await pilot.pause(0.05)  # отложенное прижатие attach отработало
        # Пользователь ушёл вверх.
        trace.scroll_to(y=3, animate=False)
        await pilot.pause(0.05)
        scr._poll_active()  # тик замечает уход вверх
        assert scr._stick_bottom is False
        y_before = trace.scroll_y
        assert y_before == 3
        _compact_ticking(thread, scr, "S1", _user("u29", "реплика 29"))
        await pilot.pause(0.05)

        assert scr._stick_bottom is False
        assert trace.scroll_y == y_before


def _subagent_thread(tmp_path: Path) -> _Thread:
    thread = _Thread(tmp_path)
    thread.append(
        _user("u1", "делегируй"),
        _call("fc1", "call_sub_1", name="subagent_worker"),
        _out("fo1", "call_sub_1", '{"ok": true, "result": "done"}'),
    )
    for kind, payload in (
        (
            "call",
            {
                "type": "function_call",
                "name": "fs_read_file",
                "arguments": '{"path": "a.py"}',
                "call_id": "call_child_9",
            },
        ),
        (
            "result",
            {
                "type": "function_call_output",
                "call_id": "call_child_9",
                "output": "ok",
            },
        ),
    ):
        append_subagent_trace_record(
            thread.dir,
            _SYS,
            _TID,
            parent_call_id="call_sub_1",
            child_run_id="run_c1",
            kind=kind,
            payload=payload,
        )
    return thread


@pytest.mark.asyncio
async def test_nested_subagent_rows_unchanged_by_compaction(tmp_path):
    thread = _subagent_thread(tmp_path)
    async with _session(tmp_path) as (app, pilot, scr):
        trace = app.screen.query_one("#trace")
        nested = {k: list(v) for k, v in scr._nested_widgets_by_parent.items()}
        assert nested.get("call_sub_1")
        _compact_ticking(thread, scr, "S1", _user("u2", "дальше"))
        await pilot.pause(0.05)
        after = {k: list(v) for k, v in scr._nested_widgets_by_parent.items()}

        assert after == nested
        assert all(w in trace.children for ws in after.values() for w in ws)
