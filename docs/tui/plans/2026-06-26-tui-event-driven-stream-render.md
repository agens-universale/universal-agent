# Событийная перерисовка стрима — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Освежать контент стрим-виджета по приходу дельты (коалесцируя до ~80 мс), а не общим poll-таймером — плавный поток без разгона тяжёлого poll.

**Architecture:** `StreamPump` оборачивает `on_stream_delta`: кладёт в `StreamSink` (как сейчас) + троттл-сигнал в UI через `call_from_thread`. Poll возвращается на 250 мс и владеет структурой (монтаж/снятие/смена kind) + служит backstop'ом; контент освежает общий `MainScreen._refresh_stream_content` (зовётся и poll'ом, и событием).

**Tech Stack:** Python 3.10+, Textual (`call_from_thread`), pytest (TUI → `uv run pytest`).

**Spec:** [docs/tui/specs/2026-06-26-tui-event-driven-stream-render-design.md](../specs/2026-06-26-tui-event-driven-stream-render-design.md)

## Global Constraints

- Poll — структура (mount/switch/clear) + backstop; событие — контент. Poll возвращается на **250 мс** (откат триала 0.4).
- Константа `STREAM_REFRESH_MS = 80` (в `stream_pump.py`); конфиг-флага НЕТ.
- Мост поток→UI — `call_from_thread` (прецедент confirm-баннера, app.py:125); `signal` в pump обёрнут try/except (teardown не роняет worker).
- `_refresh_stream_content` — контент **уже смонтированного** виджета; НЕ монтирует/снимает/переключает. Слоты `_stream_*` трогаются только на UI-потоке.
- pump TUI-only (`on_stream_delta` подключается лишь в TUI `app.py`); CLI/server/swarm не затронуты.
- Абсолютные импорты от `src/`. TUI-тесты — `uv run pytest`. Линтера нет.
- Коммиты — лаконичные, по-русски/смешанно; без AI-атрибуции, без `Co-Authored-By`; `git add` только перечисленные пути; не коммитить `.claude/`/`CLAUDE.md`. Ветка `feat/agent-instructions-and-gc-guard`; не пушить.

---

### Task 1: `StreamPump` — троттл-обёртка дельт

**Files:**
- Create: `src/tui/services/stream_pump.py`
- Test: `tests/tui/test_stream_pump.py` (new)

**Interfaces:**
- Produces: `STREAM_REFRESH_MS = 80`; `StreamPump(sink, signal, *, throttle_s, clock=time.monotonic)` с `on_delta(kind, text)` — `sink.push(kind, text)` + leading-edge троттл-сигнал (не чаще раза в `throttle_s`), сигнал-исключения проглатываются.

- [ ] **Step 1: Failing-тест**

Создать `tests/tui/test_stream_pump.py`:

```python
from tui.services.stream_pump import StreamPump, STREAM_REFRESH_MS


class _Sink:
    def __init__(self):
        self.pushed = []

    def push(self, kind, text):
        self.pushed.append((kind, text))


def test_pushes_every_delta_but_throttles_signal():
    t = [100.0]
    sink = _Sink()
    sigs = []
    pump = StreamPump(sink, lambda: sigs.append(t[0]),
                      throttle_s=0.08, clock=lambda: t[0])
    pump.on_delta("answer", "a")     # first → signal (leading-edge)
    pump.on_delta("answer", "b")     # same time → throttled
    t[0] = 100.05
    pump.on_delta("answer", "c")     # +50ms < 80ms → throttled
    t[0] = 100.20
    pump.on_delta("answer", "d")     # +200ms since last signal → signal
    assert sink.pushed == [("answer", "a"), ("answer", "b"),
                           ("answer", "c"), ("answer", "d")]
    assert len(sigs) == 2            # 4 deltas, 2 signals


def test_signal_exception_does_not_propagate():
    pump = StreamPump(_Sink(),
                      lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                      throttle_s=0.0, clock=lambda: 1.0)
    pump.on_delta("answer", "x")     # must not raise


def test_default_constant_is_80ms():
    assert STREAM_REFRESH_MS == 80
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_stream_pump.py -v`
Expected: FAIL — модуль `stream_pump` не существует.

- [ ] **Step 3: Реализовать**

Создать `src/tui/services/stream_pump.py`:

```python
"""StreamPump — событийная коалесценс-перерисовка стрима (TUI).

Оборачивает on_stream_delta: кладёт дельту в StreamSink (как раньше) и не чаще
раза в `throttle_s` дёргает signal() для перерисовки UI. Троттл-состояние живёт
на worker-потоке (агент-поток) — один поток, без локов; StreamSink уже
thread-safe. Презентация, не бизнес-логика."""
from __future__ import annotations

import time
from typing import Any, Callable

STREAM_REFRESH_MS = 80   # потолок коалесценса событийной перерисовки стрима


class StreamPump:
    def __init__(self, sink: Any, signal: Callable[[], None], *,
                 throttle_s: float,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._sink = sink
        self._signal = signal
        self._throttle_s = throttle_s
        self._clock = clock
        self._last = clock() - throttle_s   # первая дельта сразу сигналит

    def on_delta(self, kind: str, text: str) -> None:
        self._sink.push(kind, text)
        now = self._clock()
        if now - self._last >= self._throttle_s:
            self._last = now
            try:
                self._signal()
            except Exception:
                pass   # поздний сигнал при teardown loop'а не должен ронять worker
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/tui/test_stream_pump.py -v`
Expected: PASS (3).

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/stream_pump.py tests/tui/test_stream_pump.py
git commit -m "feat(tui): StreamPump — троттл-обёртка дельт стрима (коалесценс)"
```

---

### Task 2: `MainScreen._refresh_stream_content` + рефактор poll-ветки + откат poll на 250 мс

**Files:**
- Modify: `src/tui/screens/main.py` (новый метод; стрим-ветка `_poll_active` ~1527-1551; poll-интервал ~356)
- Test: `tests/tui/test_refresh_stream_content.py` (new)

**Interfaces:**
- Consumes: `render_prose`, `format_stream_block`, `format_reasoning_line` (существуют); слоты `_stream_answer_row`/`_stream_block`.
- Produces: `MainScreen._refresh_stream_content()` — освежает контент смонтированного стрим-виджета из снапшота sink (+ stick-to-bottom); no-op если ничего не смонтировано/нет sink.

- [ ] **Step 1: Failing-тест**

Создать `tests/tui/test_refresh_stream_content.py`:

```python
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _FakeRunner:
    def is_running(self) -> bool: return True
    def stop(self) -> None: pass


@pytest.mark.asyncio
async def test_refresh_updates_mounted_answer_row(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    tid = "ref1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions",
                         "tui_stream_display": "answer"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        pilot.app._stream_sink.push("answer", "первая часть")
        await pilot.pause(0.7)                       # poll смонтировал answer-row
        assert scr._stream_answer_row is not None
        pilot.app._stream_sink.push("answer", " и вторая")
        scr._refresh_stream_content()                # прямой вызов (как событие)
        content = scr._stream_answer_row.query_one(".trace-content", Static)
        assert "и вторая" in str(content.render())


@pytest.mark.asyncio
async def test_refresh_noop_when_nothing_mounted(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "ref2"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._refresh_stream_content()                # ни виджета — без исключений
        assert scr._stream_answer_row is None
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_refresh_stream_content.py -v`
Expected: FAIL — метод `_refresh_stream_content` не существует.

- [ ] **Step 3: Добавить `_refresh_stream_content`**

В `src/tui/screens/main.py` добавить метод (рядом с `_clear_stream_block`, ~694):

```python
    def _refresh_stream_content(self) -> None:
        """Освежить контент уже смонтированного стрим-виджета из снапшота sink.
        Зовётся poll-веткой (после mount/switch) и событийно (StreamPump → app →
        call_from_thread). НЕ монтирует/снимает/переключает — это poll."""
        sink = getattr(self.app, "_stream_sink", None)
        if sink is None:
            return
        from tui.services.stream_sink import format_stream_block, format_reasoning_line
        from tui.services.markdown_render import render_prose
        following = self._trace_is_at_bottom()
        think, answer = sink.snapshot()
        if self._stream_answer_row is not None:
            self._stream_answer_row.set_content(render_prose(format_stream_block(answer=answer)))
        elif self._stream_block is not None:
            self._stream_block.set_text(format_reasoning_line(think=think), thinking=True)
        else:
            return
        if following:
            self.query_one("#trace", VerticalScroll).scroll_end(animate=False)
```

- [ ] **Step 4: Рефактор стрим-ветки `_poll_active` — структура + делегат контента**

Заменить строки 1527-1551 (от `from tui.widgets.trace_row import TraceRow` до второго `if following: ... scroll_end(...)`) на:

```python
                from tui.widgets.trace_row import TraceRow
                if kind == "answer":
                    if self._stream_block is not None:          # switch reasoning→answer
                        self._stream_block.remove()
                        self._stream_block = None
                    if self._stream_answer_row is None:
                        self._stream_answer_row = TraceRow(
                            prefix="● ", content=render_prose(clean),
                            kind_class="line-agent", focusable=False)
                        self._trace_mount(self._stream_answer_row, scroll=True)
                    self._refresh_stream_content()
                elif kind == "reasoning":
                    if self._stream_answer_row is not None:      # switch answer→reasoning
                        self._stream_answer_row.remove()
                        self._stream_answer_row = None
                    if self._stream_block is None:
                        self._stream_block = StreamBlock()
                        self._trace_mount(self._stream_block, scroll=True)
                    self._refresh_stream_content()
                else:
                    self._clear_stream_block(reset_sink=False)
```

(строки 1523-1526 `mode`/`clean`/`reasoning`/`kind` остаются; `following`/inline-scroll переехали в `_refresh_stream_content`.)

- [ ] **Step 5: Откат триала — poll на 250 мс**

В `src/tui/screens/main.py` (~356) вернуть:

```python
        self.set_interval(0.25, self._poll_active)   # 250 ms — single source of truth
```

- [ ] **Step 6: Тесты зелёные + регресс стрима**

Run: `uv run pytest tests/tui/test_refresh_stream_content.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py -v`
Expected: PASS (poll по-прежнему mount/switch + автоскролл через общий метод).

- [ ] **Step 7: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_refresh_stream_content.py
git commit -m "feat(tui): _refresh_stream_content (контент стрима) + poll на 250 мс; контент общий для poll и события"
```

---

### Task 3: Проводка `StreamPump` в `app.py` + делегат `_stream_signal`

**Files:**
- Modify: `src/tui/app.py:104-107` (pump + on_stream_delta) и добавить метод-делегат рядом с `_present_confirm` (~119)
- Test: `tests/tui/test_app_stream_signal.py` (new)

**Interfaces:**
- Consumes: `StreamPump`/`STREAM_REFRESH_MS` (Task 1); `MainScreen._refresh_stream_content` (Task 2).
- Produces: `on_stream_delta = pump.on_delta`; `TuiApp._stream_signal()` (UI-поток) — делегат на `self.screen._refresh_stream_content` (no-op, если экран без метода).

- [ ] **Step 1: Failing-тест**

Создать `tests/tui/test_app_stream_signal.py`:

```python
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "running",
            "updated_at": "2026-06-15T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_stream_signal_delegates_to_active_screen(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "sig1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        calls = []
        monkeypatch.setattr(scr, "_refresh_stream_content", lambda: calls.append(1))
        pilot.app._stream_signal()
        assert calls == [1]
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_app_stream_signal.py -v`
Expected: FAIL — `_stream_signal` не существует.

- [ ] **Step 3: Делегат `_stream_signal`**

В `src/tui/app.py` добавить рядом с `_present_confirm` (~119, тот же паттерн `getattr(self.screen, …)`):

```python
    def _stream_signal(self) -> None:
        """Вызывается на UI-потоке (StreamPump → call_from_thread); событийная
        перерисовка контента стрима на активном экране (no-op без метода)."""
        fn = getattr(self.screen, "_refresh_stream_content", None)
        if callable(fn):
            fn()
```

- [ ] **Step 4: Проводка pump в `_build_real_executor`**

В `src/tui/app.py` заменить строки 104-107:

```python
        ctx = create_run_context(
            cfg, auth_connector=auth, on_progress=progress.update,
            on_stream_delta=self._stream_sink.push,
        )
```

на:

```python
        from tui.services.stream_pump import StreamPump, STREAM_REFRESH_MS
        self._stream_pump = StreamPump(
            self._stream_sink,
            signal=lambda: self.call_from_thread(self._stream_signal),
            throttle_s=STREAM_REFRESH_MS / 1000,
        )
        ctx = create_run_context(
            cfg, auth_connector=auth, on_progress=progress.update,
            on_stream_delta=self._stream_pump.on_delta,
        )
```

- [ ] **Step 5: Тесты зелёные + регресс**

Run: `uv run pytest tests/tui/test_app_stream_signal.py tests/tui/test_app_stream_wiring.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py -v`
Expected: PASS (стрим-тесты кормят `_stream_sink.push` напрямую — pump-обвязка их не затрагивает; делегат покрыт новым тестом).

- [ ] **Step 6: Commit**

```bash
git add src/tui/app.py tests/tui/test_app_stream_signal.py
git commit -m "feat(tui): проводка StreamPump (событийный сигнал перерисовки) + делегат _stream_signal"
```

---

### Task 4: Финальная сверка

- [ ] **Step 1: Прогнать весь затронутый набор**

Run: `uv run pytest tests/tui/test_stream_pump.py tests/tui/test_refresh_stream_content.py tests/tui/test_app_stream_signal.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py tests/tui/test_app_stream_wiring.py -q`
Expected: PASS.

- [ ] **Step 2: Убедиться, что триал откатан**

Run: `grep -n "set_interval(0\." src/tui/screens/main.py`
Expected: показывает `0.25` (НЕ `0.4`/`0.1`).

- [ ] **Step 3 (опц.): доки**

```bash
git add docs/tui/specs/2026-06-26-tui-event-driven-stream-render-design.md docs/tui/plans/2026-06-26-tui-event-driven-stream-render.md
git commit -m "docs(tui): дизайн+план — событийная перерисовка стрима"
```

---

## Self-Review

**Spec coverage:**
- §StreamPump (троттл, sink.push, try/except) → Task 1. ✓
- §проводка app.py (on_stream_delta=pump.on_delta, signal=call_from_thread) → Task 3. ✓
- §`_refresh_stream_content` (контент смонтированного, no-op) + poll владеет структурой + общий метод → Task 2. ✓
- §poll назад на 250 мс (откат триала) → Task 2 Step 5 + Task 4 Step 2. ✓
- §backstop (poll 250 мс) → следствие Task 2 (poll сохранён). ✓
- §регресс: существующие стрим-тесты зелёные (кормят sink напрямую, минуя pump) → Task 2/3 Step 6/5; CLI/server не затронуты (pump только в TUI `_build_real_executor`) → Task 3. ✓
- §тесты: StreamPump (Task 1), `_refresh_stream_content` (Task 2), делегат (Task 3). ✓

**Placeholder scan:** заглушек нет; код и команды приведены.

**Type consistency:** `StreamPump(sink, signal, *, throttle_s, clock)`/`on_delta(kind,text)` (Task 1) ↔ проводка (Task 3); `_refresh_stream_content()` (Task 2) ↔ делегат `_stream_signal` (Task 3); `STREAM_REFRESH_MS` (Task 1) ↔ `throttle_s=STREAM_REFRESH_MS/1000` (Task 3). Согласовано.
