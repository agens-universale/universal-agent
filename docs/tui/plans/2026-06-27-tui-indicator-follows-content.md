# Индикатор «идёт за контентом» трейса — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `#trace` сжимается до высоты контента (с динамическим потолком = доступное место), чтобы докнутый индикатор шёл сразу за контентом, опускаясь к низу над вводом лишь когда контент заполнил экран.

**Architecture:** `#trace` `height: 1fr` → `height: auto` + динамический `max-height` (ставится из кода). Новый `#spacer (1fr)` между `#indicator-bar` и `#input-row` забирает остаток, удерживая ввод/футер внизу при коротком трейсе. Рост контента до потолка держит сам Textual; наш код считает только потолок (на гарде, из `_poll_active`). Индикатор остаётся в отдельном `#indicator-bar` → наложения стрима нет.

**Tech Stack:** Python 3.10+, Textual, pytest (TUI → `uv run pytest`).

**Spec:** [docs/tui/specs/2026-06-27-tui-indicator-follows-content-design.md](../specs/2026-06-27-tui-indicator-follows-content-design.md)

## Global Constraints

- `#trace`: `height: auto` (было `1fr`); `max-height` — из кода (НЕ в CSS).
- Новый `#spacer (height: 1fr)` между `#indicator-bar` и `#input-row`.
- Потолок: `available = screen_h − header_h − input_row_h − footer_h − indicator_bar_h`; ставится `#trace.styles.max_height = available` только при изменении (гард). Рост контента — Textual, не код.
- Индикатор/`#indicator-bar`, автоскролл, частоту стрима — НЕ трогаем.
- Абсолютные импорты от `src/`. TUI-тесты — `uv run pytest`. Линтера нет.
- Коммиты — лаконичные, по-русски/смешанно; без AI-атрибуции, без `Co-Authored-By`; `git add` только перечисленные пути; не коммитить `.claude/`/`CLAUDE.md`. Ветка `feat/agent-instructions-and-gc-guard`; не пушить.

---

### Task 1: `#trace` auto + динамический потолок + `#spacer`

**Files:**
- Modify: `src/tui/screens/main.py` (`compose` ~180-182; init-поле ~119; новый `_sync_trace_max_height`; вызов в `_poll_active` ~1390)
- Modify: `src/tui/styles.tcss` (`#trace` height; `#spacer` новый)
- Test: `tests/tui/test_indicator_follows_content.py` (new)

**Interfaces:**
- Produces: `#spacer` (1fr) распорка; `MainScreen._sync_trace_max_height()` — ставит `#trace.styles.max_height = available` (гард по `self._trace_max_height`).
- Consumes: `Vertical`, `VerticalScroll` (импортированы), размеры виджетов через `.size.height`.

- [ ] **Step 1: Failing-тесты (структурный инвариант + гард)**

Создать `tests/tui/test_indicator_follows_content.py`:

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
async def test_short_content_trace_shrinks_indicator_follows(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import Vertical, VerticalScroll
    from tui.app import TuiApp

    tid = "fc1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": "короткий ответ"}}, live=True)
        await pilot.pause(0.4)                  # poll-тик → _sync_trace_max_height
        trace = pilot.app.query_one("#trace", VerticalScroll)
        spacer = pilot.app.query_one("#spacer", Vertical)
        assert trace.max_scroll_y == 0          # контент влез — нет скролла
        assert spacer.size.height > 0           # спейсер забрал низ → индикатор НЕ у низа


@pytest.mark.asyncio
async def test_tall_content_trace_caps_indicator_at_bottom(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import Vertical, VerticalScroll
    from tui.app import TuiApp

    tid = "fc2"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        big = "\n".join(f"строка {i}" for i in range(60))
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": big}}, live=True)
        await pilot.pause(0.4)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        spacer = pilot.app.query_one("#spacer", Vertical)
        footer = pilot.app.query_one("#footer")
        assert trace.max_scroll_y > 0           # контент не влез — скроллится
        assert spacer.size.height == 0          # слака нет → индикатор у низа
        assert footer.region.bottom == 24       # футер на самом низу (не выдавлен)


@pytest.mark.asyncio
async def test_sync_max_height_guard_stable(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    tid = "fc3"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test(size=(80, 24)) as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.4)
        first = scr._trace_max_height
        scr._sync_trace_max_height()            # без изменения размеров
        assert scr._trace_max_height == first   # стабильно (гард)
        assert first is not None and first > 0
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_indicator_follows_content.py -v`
Expected: FAIL — `#spacer` нет (query_one бросает) / `_trace_max_height`/`_sync_trace_max_height` нет / `#trace` всё ещё `1fr` (spacer не отделён).

- [ ] **Step 3: `compose()` — добавить `#spacer`**

В `src/tui/screens/main.py` `compose()` (между `yield Vertical(id="indicator-bar")` ~181 и `with Horizontal(id="input-row")` ~182):

```python
            yield Vertical(id="indicator-bar")
            yield Vertical(id="spacer")
            with Horizontal(id="input-row"):
```

- [ ] **Step 4: init-поле + метод `_sync_trace_max_height`**

В `__init__` рядом с `self._thinking = None` (~119) добавить:

```python
        self._trace_max_height = None  # type: Optional[int]
```

Добавить метод (рядом с `_trace_is_at_bottom` / `_trace_mount`, ~736):

```python
    def _sync_trace_max_height(self) -> None:
        """#trace сжимается до контента: потолок = доступное место (экран минус
        header/input/footer/indicator-bar). Рост контента до потолка держит Textual;
        здесь только сам потолок, на гарде (set лишь при изменении значения)."""
        try:
            screen_h = self.size.height
            header_h = self.query_one("#header").size.height
            input_h = self.query_one("#input-row").size.height
            footer_h = self.query_one("#footer").size.height
            bar_h = self.query_one("#indicator-bar").size.height
        except Exception:
            return
        available = screen_h - header_h - input_h - footer_h - bar_h
        if available <= 0:
            return
        if available != self._trace_max_height:
            self._trace_max_height = available
            self.query_one("#trace", VerticalScroll).styles.max_height = available
```

- [ ] **Step 5: Вызов из `_poll_active`**

В начале `_poll_active` (~1390-1392), первой строкой тела:

```python
    def _poll_active(self) -> None:
        self._sync_trace_max_height()
        # Отложенное удаление осиротевших реплик (cancel-with-undo), когда idle.
        self._run_orphan_cleanups()
```

- [ ] **Step 6: CSS — `#trace` auto + `#spacer`**

В `src/tui/styles.tcss`: строку `#trace { height: 1fr; padding: 0 1; background: $bg; }` (~41) →
`#trace { height: auto; padding: 0 1; background: $bg; }`. И добавить рядом:

```
#spacer { height: 1fr; background: $bg; }
```

- [ ] **Step 7: Тесты зелёные**

Run: `uv run pytest tests/tui/test_indicator_follows_content.py -v`
Expected: PASS (3). Если `test_tall_content...` падает на `footer.region.bottom == 24` (футер выдавлен) — значит `available` завышен; уточнить формулу (вычесть border `#input-row`/`#footer`, если `.size.height` их не учитывает) до зелёного, НЕ ослабляя проверки. Если механика Textual не даёт сжатия/скролла как ожидается — STOP, DONE_WITH_CONCERNS с точным наблюдением.

- [ ] **Step 8: Регресс автоскролла/индикатора/стрима**

Run: `uv run pytest tests/tui/test_trace_autoscroll.py tests/tui/test_indicator_dock.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py tests/tui/test_refresh_stream_content.py tests/tui/test_thinking_indicator.py -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/tui/screens/main.py src/tui/styles.tcss tests/tui/test_indicator_follows_content.py
git commit -m "feat(tui): индикатор идёт за контентом — #trace auto + динамический потолок + spacer"
```

---

### Task 2: Финальная сверка

- [ ] **Step 1: Прогнать затронутый набор**

Run: `uv run pytest tests/tui/test_indicator_follows_content.py tests/tui/test_trace_autoscroll.py tests/tui/test_indicator_dock.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py tests/tui/test_refresh_stream_content.py tests/tui/test_thinking_indicator.py tests/tui/test_thinking_persist.py tests/tui/test_resume_terminal_indicator.py -q`
Expected: PASS.

- [ ] **Step 2: Убедиться, что `#trace` больше не `1fr`**

Run: `grep -n "#trace {" src/tui/styles.tcss`
Expected: показывает `height: auto` (не `1fr`).

- [ ] **Step 3 (опц.): доки**

```bash
git add docs/tui/specs/2026-06-27-tui-indicator-follows-content-design.md docs/tui/plans/2026-06-27-tui-indicator-follows-content.md
git commit -m "docs(tui): дизайн+план — индикатор идёт за контентом трейса"
```

---

## Self-Review

**Spec coverage:**
- §`#trace` auto + динамический max-height → Task 1 Step 4/6. ✓
- §`#spacer` (1fr) между indicator-bar и input → Task 1 Step 3/6. ✓
- §формула `available` + гард + вызов из `_poll_active` → Task 1 Step 4/5. ✓
- §рост контента держит Textual (не на каждый тик) → метод считает только потолок; контент — auto. ✓
- §автоскролл/мерцание не трогаем → ни автоскролл, ни `#indicator-bar` не меняются. ✓
- §верификация (прототип) → встроена в TDD (Step 7: тесты = проверка механики; off-by-one формулы тюнится до зелёного). ✓
- §тесты (короткий → spacer>0/no-scroll; высокий → spacer==0/scroll/футер внизу; гард) → Task 1 Step 1. ✓

**Placeholder scan:** заглушек нет; код и команды приведены.

**Type consistency:** `_trace_max_height: Optional[int]` (Step 4) ↔ гард в `_sync_trace_max_height` ↔ тест `test_sync_max_height_guard_stable`; `#spacer`/`#trace`/`#indicator-bar` — id согласованы между compose, CSS, методом и тестами.
