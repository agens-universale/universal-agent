# Индикатор-статус вне скролла (dock) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Вынести `ThinkingIndicator` из скролл-контейнера `#trace` в фиксированную строку `#indicator-bar` между `#trace` и `#input-row`, чтобы растущий стрим не накладывался на индикатор (мерцание).

**Architecture:** Атомарная правка верстки: добавить контейнер `#indicator-bar` в `compose()`, перенаправить монтаж индикатора туда (вместо `#trace`), развязать `#trace` от индикатора (`_trace_mount` без `before=self._thinking`), поправить CSS-выравнивание. Жизненный цикл индикатора (state/hide/destroy/persist) не меняется.

**Tech Stack:** Python 3.10+, Textual, pytest (TUI → `uv run pytest`).

**Spec:** [docs/tui/specs/2026-06-26-tui-indicator-dock-design.md](../specs/2026-06-26-tui-indicator-dock-design.md)

## Global Constraints

- Индикатор монтируется в `#indicator-bar` (фикс. строка между `#trace` и `#input-row`), НЕ в `#trace`.
- `#indicator-bar` выровнен с контентом трейса: `padding: 0 1` (как `#trace`), `height: auto` (0 в простое), `background: $bg`.
- `_trace_mount` больше не использует `before=self._thinking` (индикатора в `#trace` нет).
- Жизненный цикл индикатора (set_state/hide/destroy/terminal-persist/resume), анимация — НЕ трогаем. Меняется только родитель монтажа + убираются scroll-to-indicator вызовы.
- Абсолютные импорты от `src/`. TUI-тесты — `uv run pytest`. Линтера нет.
- Коммиты — лаконичные, по-русски/смешанно; без AI-атрибуции, без `Co-Authored-By`; `git add` только перечисленные пути; не коммитить `.claude/`/`CLAUDE.md`. Ветка `feat/agent-instructions-and-gc-guard`; не пушить.

---

### Task 1: Док индикатора — `#indicator-bar`, перенос монтажа, развязка `#trace`, CSS

**Files:**
- Modify: `src/tui/screens/main.py` (`compose` ~180; `_show_thinking_indicator` ~619-644; `_restore_terminal_indicator` ~677-680; `_trace_mount` ~750-756)
- Modify: `src/tui/styles.tcss` (`#indicator-bar` новый; `.thinking-indicator` ~200)
- Test: `tests/tui/test_indicator_dock.py` (new)

**Interfaces:**
- Produces: контейнер `#indicator-bar` (Vertical) — родитель индикатора; `_trace_mount` монтирует строки в `#trace` без `before=`.
- Consumes: `Vertical` (уже импортирован в main.py:19), `ThinkingIndicator`.

- [ ] **Step 1: Failing-тесты (структурный инвариант)**

Создать `tests/tui/test_indicator_dock.py`:

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
async def test_indicator_docked_not_in_trace(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import Vertical, VerticalScroll
    from tui.app import TuiApp
    from tui.widgets.thinking_indicator import ThinkingIndicator

    tid = "dock1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        bar = pilot.app.query_one("#indicator-bar", Vertical)
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(isinstance(w, ThinkingIndicator) for w in bar.children)
        assert not any(isinstance(w, ThinkingIndicator) for w in trace.children)


@pytest.mark.asyncio
async def test_trace_row_lands_in_trace_with_indicator_docked(tmp_path):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    from tui.widgets.thinking_indicator import ThinkingIndicator
    from tui.widgets.trace_row import TraceRow

    tid = "dock2"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._show_thinking_indicator(state="thinking")
        scr._render_record({"payload": {"id": "a1", "role": "assistant",
                                        "content": "привет"}}, live=True)
        await pilot.pause()
        trace = pilot.app.query_one("#trace", VerticalScroll)
        assert any(isinstance(w, TraceRow) for w in trace.children)        # строка в трейсе
        assert not any(isinstance(w, ThinkingIndicator) for w in trace.children)  # индикатор — нет
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_indicator_dock.py -v`
Expected: FAIL — `#indicator-bar` не существует (query_one бросает) / индикатор в `#trace`.

- [ ] **Step 3: `compose()` — добавить `#indicator-bar`**

В `src/tui/screens/main.py` `compose()` (между `yield VerticalScroll(id="trace")` ~180 и `with Horizontal(id="input-row")` ~181):

```python
            yield VerticalScroll(id="trace")
            yield Vertical(id="indicator-bar")
            with Horizontal(id="input-row"):
```

- [ ] **Step 4: `_show_thinking_indicator` — монтаж в `#indicator-bar`, без scroll-to-indicator**

В unhide-ветке (~625-630) убрать `following`/scroll_end:

```python
            self._thinking.remove_class("hidden")
            self._thinking.set_state(state)
            return
```

В mount-ветке (~639-644) заменить монтаж в `#trace` на `#indicator-bar`:

```python
        self._thinking = indicator
        self.query_one("#indicator-bar", Vertical).mount(indicator)
```

(удаляются `trace = self.query_one("#trace", …)`, `following = …`, `trace.scroll_end(...)`.)

- [ ] **Step 5: `_restore_terminal_indicator` — монтаж в `#indicator-bar`**

(~677-680) заменить:

```python
        self._thinking = ind
        self.query_one("#indicator-bar", Vertical).mount(ind)
```

(удаляются `trace = self.query_one("#trace", …)`, `trace.mount(ind)`, `trace.scroll_end(...)`.)

- [ ] **Step 6: `_trace_mount` — без `before=self._thinking`**

(~750-756) тело привести к:

```python
        trace = self.query_one("#trace", VerticalScroll)
        following = force_scroll or self._trace_is_at_bottom()
        trace.mount(widget)
        if scroll and following:
            trace.scroll_end(animate=False)
```

И из докстринга убрать фразу про «индикатор всегда последний» (строки ~744-745).

- [ ] **Step 7: CSS**

В `src/tui/styles.tcss`: после строки `#trace { height: 1fr; padding: 0 1; background: $bg; }` (~41) добавить:

```
#indicator-bar { height: auto; padding: 0 1; background: $bg; }
```

И строку `.thinking-indicator` (~200) — убрать `margin-top: 1`:

```
.thinking-indicator { color: $deep-dim; padding: 0 1 0 0; height: 1; }
```

- [ ] **Step 8: Тесты зелёные**

Run: `uv run pytest tests/tui/test_indicator_dock.py -v`
Expected: PASS (2).

- [ ] **Step 9: Регресс индикатора + трейса**

Run: `uv run pytest tests/tui/test_thinking_indicator.py tests/tui/test_thinking_indicator_tail.py tests/tui/test_thinking_persist.py tests/tui/test_processing_indicator.py tests/tui/test_resume_terminal_indicator.py tests/tui/test_thinking_indicator_finalize.py tests/tui/test_indicator_status_wiring.py tests/tui/test_trace_autoscroll.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py -q`
Expected: PASS. Если тест ассертит, что индикатор — ребёнок `#trace` или его порядок относительно строк трейса, обновить на `#indicator-bar` (НЕ ослабляя смысл проверки). Если падение по иной причине (реальный регресс), STOP — DONE_WITH_CONCERNS с точной ошибкой.

- [ ] **Step 10: Commit**

```bash
git add src/tui/screens/main.py src/tui/styles.tcss tests/tui/test_indicator_dock.py
git commit -m "fix(tui): индикатор — фикс-строка #indicator-bar вне #trace (нет наложения стрима)"
```

(если в Step 9 правились существующие тесты — добавить их пути в `git add`.)

---

### Task 2: Финальная сверка

- [ ] **Step 1: Прогнать весь набор индикатора/стрима**

Run: `uv run pytest tests/tui/test_indicator_dock.py tests/tui/test_thinking_indicator.py tests/tui/test_thinking_indicator_tail.py tests/tui/test_thinking_persist.py tests/tui/test_processing_indicator.py tests/tui/test_resume_terminal_indicator.py tests/tui/test_thinking_indicator_finalize.py tests/tui/test_indicator_status_wiring.py tests/tui/test_trace_autoscroll.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py tests/tui/test_refresh_stream_content.py -q`
Expected: PASS.

- [ ] **Step 2: Убедиться, что индикатора нет в `#trace`-монтаже**

Run: `grep -n "before=self._thinking\|trace.mount(indicator)\|trace.mount(ind)" src/tui/screens/main.py`
Expected: пусто (индикатор больше не монтируется в `#trace`).

- [ ] **Step 3 (опц.): доки**

```bash
git add docs/tui/specs/2026-06-26-tui-indicator-dock-design.md docs/tui/plans/2026-06-26-tui-indicator-dock.md
git commit -m "docs(tui): дизайн+план — индикатор вне скролла (dock)"
```

---

## Self-Review

**Spec coverage:**
- §размещение `#indicator-bar` между `#trace` и `#input-row` → Task 1 Step 3. ✓
- §цель монтажа → `#indicator-bar` (show + restore) → Task 1 Step 4/5. ✓
- §развязать `#trace` (`_trace_mount` без `before=`) → Task 1 Step 6. ✓
- §CSS (`#indicator-bar` height auto/padding; `.thinking-indicator` без margin-top, выравнивание по `padding: 0 1`) → Task 1 Step 7. ✓
- §жизненный цикл без изменений → set_state/hide/destroy/persist не трогаются (только родитель + scroll-to-indicator убран). ✓
- §регресс (parent-agnostic зелёные; parent/порядок-тесты обновить; автоскролл) → Task 1 Step 9 + Task 2. ✓
- §тесты (индикатор в `#indicator-bar`, не в `#trace`; строка в `#trace` без индикатора-соседа) → Task 1 Step 1. ✓

**Placeholder scan:** заглушек нет; код и команды приведены.

**Type consistency:** `#indicator-bar` (Vertical) — родитель индикатора, согласован между compose (Step 3), монтажом (Step 4/5) и тестами (Step 1); `_trace_mount` без `before=` (Step 6) ↔ инвариант «индикатор не в `#trace`» (тесты).
