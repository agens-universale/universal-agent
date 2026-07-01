# Индикатор контекста + статусы сессии — Implementation Plan (План 1 из 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Индикатор заполненности контекста в шапке TUI + точные цветные статусы сессии (running/asking/completed/stopped/failed).

**Architecture:** Всё в TUI-слое. Чистые презентационные хелперы (`render_context_meter`, `status_badge_color`) + чтение live-сегмента через новый `file_store.read_live_messages` + рендер шапки rich-Text'ом с цветами + стамп терминального статуса в `meta.tui.status` на спадающем фронте прогона. Executor/persistence/суммаризация НЕ трогаются.

**Tech Stack:** Python ≥3.10, Textual, rich.Text, pytest.

**Scope-note:** Команда `/compact` — отдельный План 2 (требует доменного входа форс-сжатия), здесь её НЕТ.

## Global Constraints

- Абсолютные импорты от `src/`. TUI-тесты — `uv run pytest`.
- Цвета индикатора порогами (по `pct = round(100*chars/threshold)`, кап 100): `pct < 60` зелёный `OK`; `60 ≤ pct ≤ 90` золотистый `RUN_COLOR`; `pct > 90` оранжевый `WARN` (`#f0883e`).
- Индикатор виден ТОЛЬКО при `backend_api_mode == "chat_completions"` И `chat_completions_summary_trigger_chars > 0`; иначе скрыт.
- Бар индикатора — 8 ячеек (`▓` заполнено / `░` пусто). Текст: `контекст <bar> NN%` (слово, без абсолютного счётчика).
- Статусы (EN) и цвета: `running`→`RUN_COLOR`, `asking`→`ACCENT` (teal), `completed`→`OK`, `stopped`→`DIM`, `failed`→`ERR`.
- Терминальный статус пишется в `meta.tui.status` (TUI-owned), бейдж читает его приоритетно.
- `palette.py` правит ОБА файла только если значение есть в TCSS; `WARN` рендерится через Python rich.Text (CSS его не использует) → правим только `palette.py`.

## File Structure

- Create `src/tui/services/context_meter.py` — `render_context_meter(chars, threshold) -> tuple[str,str] | None`.
- Create `src/tui/services/status_badge.py` — `status_badge_color(badge: str) -> str`.
- Modify `src/tui/palette.py` — `WARN = "#f0883e"`.
- Modify `src/tui/services/stores/file_store.py` — `read_live_messages(task_id, system=None) -> list[dict]`.
- Modify `src/tui/screens/main.py` — `_task_status_badge` (meta.tui.status), `_compose_header_segments`/`_refresh_header` (rich render + meter), `_poll_active` (стамп терминала), call-sites `== "[stopped]"`.
- Modify `src/tui/services/agent_runner.py` — `stop_requested` property.
- Tests: `tests/tui/test_context_meter.py`, `test_status_badge.py`, `test_read_live_messages.py`, `test_header_status_render.py`, `test_terminal_status_stamp.py`.

---

## Task 1: Палитра `WARN` + хелпер индикатора

**Files:**
- Modify: `src/tui/palette.py`
- Create: `src/tui/services/context_meter.py`
- Test: `tests/tui/test_context_meter.py`

**Interfaces:**
- Produces: `render_context_meter(chars: int, threshold: int) -> tuple[str, str] | None` — `(text, color)` или `None` (скрыт при `threshold <= 0`).

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tui/test_context_meter.py`:

```python
"""render_context_meter: текст «контекст <bar> NN%» + цвет порогами."""
import pytest

from tui.services.context_meter import render_context_meter
from tui.palette import OK, RUN_COLOR, WARN


def test_hidden_when_threshold_zero():
    assert render_context_meter(100, 0) is None


@pytest.mark.parametrize("chars,threshold,pct,color", [
    (0, 1000, 0, OK),
    (590, 1000, 59, OK),
    (600, 1000, 60, RUN_COLOR),
    (900, 1000, 90, RUN_COLOR),
    (910, 1000, 91, WARN),
    (1000, 1000, 100, WARN),
    (5000, 1000, 100, WARN),   # over threshold → cap 100%
])
def test_pct_and_color(chars, threshold, pct, color):
    text, col = render_context_meter(chars, threshold)
    assert f"{pct}%" in text
    assert text.startswith("контекст ")
    assert col == color


def test_bar_is_eight_cells():
    text, _ = render_context_meter(500, 1000)  # 50%
    bar = text.split(" ")[1]
    assert len(bar) == 8
    assert set(bar) <= {"▓", "░"}
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_context_meter.py -q`
Expected: FAIL — `ModuleNotFoundError: tui.services.context_meter` / `ImportError: WARN`.

- [ ] **Step 3: Добавить `WARN` в палитру**

В [src/tui/palette.py](../../../src/tui/palette.py) после `ERR = "#C75C54"`:

```python
ERR = "#C75C54"
WARN = "#f0883e"          # предупреждение «контекст почти полон» (>90%); НЕ ошибка
```

- [ ] **Step 4: Реализовать хелпер**

Создать `src/tui/services/context_meter.py`:

```python
"""Индикатор заполненности контекста сессии для шапки TUI (чистая презентация)."""
from __future__ import annotations

from tui.palette import OK, RUN_COLOR, WARN

_CELLS = 8


def render_context_meter(chars: int, threshold: int) -> tuple[str, str] | None:
    """`(text, color)` для индикатора или None, если показывать нечего.

    text = "контекст ▓▓▓▓▓░░░ 58%"; color — токен палитры по порогам.
    """
    if threshold <= 0:
        return None
    pct = min(100, round(100 * chars / threshold))
    filled = round(_CELLS * pct / 100)
    bar = "▓" * filled + "░" * (_CELLS - filled)
    if pct < 60:
        color = OK
    elif pct <= 90:
        color = RUN_COLOR
    else:
        color = WARN
    return f"контекст {bar} {pct}%", color
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_context_meter.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/tui/palette.py src/tui/services/context_meter.py tests/tui/test_context_meter.py
git commit -m "tui: индикатор контекста — хелпер render_context_meter + палитра WARN"
```

---

## Task 2: Хелпер цвета статуса

**Files:**
- Create: `src/tui/services/status_badge.py`
- Test: `tests/tui/test_status_badge.py`

**Interfaces:**
- Produces: `status_badge_color(badge: str) -> str` — цвет палитры по бейджу вида `"[running]"`; неизвестный → `DIM`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tui/test_status_badge.py`:

```python
"""status_badge_color: цвет палитры по тексту бейджа."""
from tui.services.status_badge import status_badge_color
from tui.palette import RUN_COLOR, ACCENT, OK, DIM, ERR


def test_known_statuses():
    assert status_badge_color("[running]") == RUN_COLOR
    assert status_badge_color("[asking]") == ACCENT
    assert status_badge_color("[completed]") == OK
    assert status_badge_color("[stopped]") == DIM
    assert status_badge_color("[failed]") == ERR


def test_unknown_falls_back_dim():
    assert status_badge_color("[whatever]") == DIM
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_status_badge.py -q`
Expected: FAIL — `ModuleNotFoundError: tui.services.status_badge`.

- [ ] **Step 3: Реализовать хелпер**

Создать `src/tui/services/status_badge.py`:

```python
"""Цвет бейджа статуса сессии (чистая презентация)."""
from __future__ import annotations

from tui.palette import ACCENT, DIM, ERR, OK, RUN_COLOR

_COLORS = {
    "[running]": RUN_COLOR,
    "[asking]": ACCENT,
    "[completed]": OK,
    "[stopped]": DIM,
    "[failed]": ERR,
}


def status_badge_color(badge: str) -> str:
    """Цвет палитры для бейджа (`"[running]"` …); неизвестный → DIM."""
    return _COLORS.get(badge, DIM)
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_status_badge.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/status_badge.py tests/tui/test_status_badge.py
git commit -m "tui: status_badge_color — цвет бейджа статуса сессии"
```

---

## Task 3: `file_store.read_live_messages`

**Files:**
- Modify: `src/tui/services/stores/file_store.py`
- Test: `tests/tui/test_read_live_messages.py`

**Interfaces:**
- Produces: `FileTaskStore.read_live_messages(task_id, system=None) -> list[dict]` — payload-словари сообщений ТОЛЬКО из live `data_<sys>_<tid>.jsonl` (без архивов), в порядке файла. Нет файла → `[]`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tui/test_read_live_messages.py`:

```python
"""read_live_messages: payloads только из live data-файла (без архивов)."""
import json

from tui.services.stores.file_store import FileTaskStore

_TID = "live0000000000000000000000000000a"


def _write(d, name, payloads):
    with open(d / name, "w", encoding="utf-8") as f:
        for i, p in enumerate(payloads, 1):
            f.write(json.dumps({"type": "message", "thread_id": _TID, "system": "cli",
                                "seq": i, "ts": "t", "payload": p}, ensure_ascii=False) + "\n")


def test_reads_live_payloads(tmp_path):
    # архив игнорируется; берём только live
    _write(tmp_path, f"data_cli_{_TID}__pre_summary__20260101_000000.jsonl",
           [{"role": "user", "content": "архив"}])
    _write(tmp_path, f"data_cli_{_TID}.jsonl",
           [{"role": "user", "content": "живой-1"}, {"role": "assistant", "content": "живой-2"}])
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    msgs = store.read_live_messages(_TID)
    assert [m["content"] for m in msgs] == ["живой-1", "живой-2"]


def test_missing_live_file_returns_empty(tmp_path):
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    assert store.read_live_messages(_TID) == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_read_live_messages.py -q`
Expected: FAIL — `AttributeError: 'FileTaskStore' object has no attribute 'read_live_messages'`.

- [ ] **Step 3: Реализовать метод**

В [src/tui/services/stores/file_store.py](../../../src/tui/services/stores/file_store.py) добавить метод (рядом с `read_segments`; использует существующие `_data_path` и `_parse_line`):

```python
    def read_live_messages(self, task_id: str, system: Optional[str] = None) -> list[dict]:
        """payload-словари сообщений ТОЛЬКО из live data-файла (без архивов).
        Для лёгкого подсчёта объёма контекста (индикатор). Нет файла → []."""
        sys_ = system or self._system
        live = self._data_path(task_id, sys_)
        if not live.exists():
            return []
        out: list[dict] = []
        with open(live, "r", encoding="utf-8") as f:
            for line in f:
                rec = self._parse_line(line.rstrip("\n"))
                payload = rec.get("payload") if isinstance(rec, dict) else None
                if isinstance(payload, dict):
                    out.append(payload)
        return out
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_read_live_messages.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/stores/file_store.py tests/tui/test_read_live_messages.py
git commit -m "tui(store): read_live_messages — payloads live data-файла для индикатора"
```

---

## Task 4: Рендер шапки — цветной статус + индикатор

**Files:**
- Modify: `src/tui/screens/main.py` (`_task_status_badge`, `_compose_header_segments`, `_refresh_header`, call-sites `== "[stopped]"`)
- Test: `tests/tui/test_header_status_render.py`

**Interfaces:**
- Consumes: `render_context_meter` (T1), `status_badge_color` (T2), `read_live_messages` (T3), `calc_history_chars_for_trigger` (`message_format`).
- Produces: `_task_status_badge` возвращает `[running]/[asking]/[completed]/[stopped]/[failed]` (читает `meta.tui.status`); шапка рендерится `rich.Text` с цветами.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/tui/test_header_status_render.py`:

```python
"""Шапка: цветной бейдж статуса + индикатор контекста (rich.Text)."""
import json
import pytest


def _write_task(tmp_path, tid, status_tui=None):
    meta = {"thread_id": tid, "system_id": "cli"}
    if status_tui is not None:
        meta["tui"] = {"status": status_tui}
    (tmp_path / f"meta_cli_{tid}.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_badge_completed_from_meta_tui(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000001a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        # нет активного runner → терминальный статус из meta.tui.status
        assert scr._task_status_badge() == "[completed]"


@pytest.mark.asyncio
async def test_header_left_is_colored_text(tmp_path):
    pytest.importorskip("textual")
    from rich.text import Text
    from tui.app import TuiApp
    from tui.palette import OK
    tid = "hdr00000000000000000000000000002a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        left, _right = scr._compose_header_segments()
        assert isinstance(left, Text)
        assert "[completed]" in left.plain
        # есть стилизованный спан с цветом OK на бейдже
        styles = " ".join(str(s.style) for s in left.spans)
        assert OK.lower() in styles.lower()


@pytest.mark.asyncio
async def test_meter_hidden_in_responses_mode(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "hdr00000000000000000000000000003a"
    _write_task(tmp_path, tid, status_tui="completed")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "responses"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        left, _ = scr._compose_header_segments()
        assert "контекст" not in left.plain  # индикатор скрыт вне chat_completions
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_header_status_render.py -q`
Expected: FAIL — `_task_status_badge` не знает `[completed]` (вернёт `[stopped]`); `left` сейчас `str`, не `Text`.

- [ ] **Step 3: Расширить `_task_status_badge`**

В [src/tui/screens/main.py](../../../src/tui/screens/main.py) заменить тело `_task_status_badge` (строки ~193-206):

```python
    def _task_status_badge(self) -> str:
        # ask_human pending — агент ждёт ответа пользователя (приоритет).
        tui_state = getattr(self.app, "tui_state", None)
        if tui_state is not None and tui_state.ask_human_state is not None:
            return "[asking]"
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and runner.is_running():
            return "[running]"
        # Нет активного runner — терминальный статус.
        if self._store is not None and self._active_task_id is not None:
            meta = self._store.get_task_meta(self._active_task_id)
            tui_status = (meta.get("tui") or {}).get("status")
            if tui_status in ("completed", "stopped", "failed"):
                return f"[{tui_status}]"
            if meta.get("status") == "running":
                return "[running]"
            if meta.get("status") == "completed":
                return "[completed]"
            if meta.get("status") in ("failed", "unknown_failed"):
                return "[failed]"
        return "[stopped]"
```

- [ ] **Step 4: Добавить хелпер индикатора и rich-рендер шапки**

В `main.py` добавить импорты (рядом с прочими `from tui...`):

```python
from rich.text import Text
from tui.services.context_meter import render_context_meter
from tui.services.status_badge import status_badge_color
from message_format import calc_history_chars_for_trigger
```

Добавить метод (рядом с `_compose_header_segments`):

```python
    def _context_meter(self) -> tuple[str, str] | None:
        """(text, color) индикатора контекста или None (скрыт)."""
        cfg = getattr(self.app, "_config", None) or {}
        if str(cfg.get("backend_api_mode")) != "chat_completions":
            return None
        threshold = int(cfg.get("chat_completions_summary_trigger_chars", 0) or 0)
        if threshold <= 0 or self._store is None or self._active_task_id is None:
            return None
        msgs = self._store.read_live_messages(self._active_task_id)
        return render_context_meter(calc_history_chars_for_trigger(msgs), threshold)
```

Заменить активную ветку `_compose_header_segments` (где `self._active_task_id is not None`) — вернуть `rich.Text`:

```python
        short_id = self._active_task_id[:8]
        status = self._task_status_badge()
        left = Text()
        left.append(f"Сессия: {short_id}  ", style=_DIM)
        left.append(status, style=status_badge_color(status))
        left.append(f"  {model_chip}", style=_DIM)
        meter = self._context_meter()
        if meter is not None:
            text, color = meter
            left.append("  ·  ", style=_DIM)
            left.append(text, style=color)
        return left, _HEADER_RIGHT
```

(`_DIM` — импортируй `from tui.palette import DIM as _DIM`, если ещё не импортирован. Empty-state ветка `self._active_task_id is None` остаётся строкой — не трогаем.)

- [ ] **Step 5: Обновить call-sites `== "[stopped]"`**

В `main.py` есть проверки финализации осиротевших вызовов по `_task_status_badge() == "[stopped]"` (около строк 1205-1208). Терминальными теперь являются и `[completed]`/`[failed]`. Заменить условие на «бейдж не живой»:

```python
        if self._active_calls and self._task_status_badge() in ("[stopped]", "[completed]", "[failed]"):
            for row in self._active_calls.values():
                row.mark_interrupted()
            self._active_calls.clear()
```

(Грепни `"[stopped]"` в `main.py` и поправь каждый такой сравнительный сайт; строковые `_mount_trace_row(prefix="[stopped]"...)` — это вывод в трейс, их НЕ трогаем.)

- [ ] **Step 6: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_header_status_render.py -q`
Expected: PASS.

- [ ] **Step 7: Прогнать смежное (шапка/поллинг не сломаны)**

Run: `uv run pytest tests/tui/test_header_status_render.py tests/tui/test_main_screen_tool_pair.py -q`
Expected: PASS (или прежний baseline без новых падений).

- [ ] **Step 8: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_header_status_render.py
git commit -m "tui: цветной бейдж статуса + индикатор контекста в шапке"
```

---

## Task 5: Стамп терминального статуса на спадающем фронте

**Files:**
- Modify: `src/tui/services/agent_runner.py` (`stop_requested` property)
- Modify: `src/tui/screens/main.py` (`_poll_active` — детект True→False, запись `meta.tui.status`)
- Test: `tests/tui/test_terminal_status_stamp.py`

**Interfaces:**
- Consumes: `AgentRunner.is_running()`, `app.last_runner_error`, `file_store.update_tui_meta` (существует).
- Produces: `AgentRunner.stop_requested` (property → bool); запись `meta.tui.status ∈ {completed, stopped, failed}` при завершении прогона.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tui/test_terminal_status_stamp.py`:

```python
"""Стамп терминального статуса в meta.tui.status на спадающем фронте прогона."""
import json
import pytest


def _write_task(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _Runner:
    def __init__(self, running, stop_requested=False):
        self._running = running
        self.stop_requested = stop_requested
    def is_running(self):
        return self._running


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_requested,last_error,expected", [
    (False, None, "completed"),
    (True, None, "stopped"),
    (False, "boom", "failed"),
])
async def test_falling_edge_stamps_status(tmp_path, stop_requested, last_error, expected):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "stmp0000000000000000000000000001a"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        # был running на прошлом тике
        scr._was_running = True
        pilot.app.active_runner = _Runner(running=False, stop_requested=stop_requested)
        pilot.app.last_runner_error = last_error
        scr._poll_active()
        await pilot.pause()
        meta = scr._store.get_task_meta(tid)
        assert (meta.get("tui") or {}).get("status") == expected
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_terminal_status_stamp.py -q`
Expected: FAIL — `meta.tui.status` не пишется (нет логики спадающего фронта).

- [ ] **Step 3: Добавить `stop_requested` в AgentRunner**

В [src/tui/services/agent_runner.py](../../../src/tui/services/agent_runner.py) добавить property (рядом с `is_running`/`stop`):

```python
    @property
    def stop_requested(self) -> bool:
        """Был ли запрошен стоп текущего/последнего прогона (для терминального статуса)."""
        return self._stop_requested
```

- [ ] **Step 4: Стамп на спадающем фронте в `_poll_active`**

В [src/tui/screens/main.py](../../../src/tui/screens/main.py) в `__init__` экрана добавить флаг (рядом с прочими `self._...`):

```python
        self._was_running: bool = False
```

В `_poll_active`, сразу после вычисления `is_running = runner is not None and runner.is_running()` (строка ~1220), добавить:

```python
        # Спадающий фронт прогона True→False → зафиксировать терминальный статус
        # в meta.tui.status (TUI-owned), чтобы бейдж пережил переоткрытие сессии.
        if self._was_running and not is_running and self._store is not None \
                and self._active_task_id is not None:
            if getattr(self.app, "last_runner_error", None):
                outcome = "failed"
            elif runner is not None and getattr(runner, "stop_requested", False):
                outcome = "stopped"
            else:
                outcome = "completed"
            self._store.update_tui_meta(self._active_task_id, {"status": outcome})
        self._was_running = is_running
```

> `is_running` остаётся True во время `ask_human` (поток жив, заблокирован) — спадающего фронта там нет, ложного `completed` не будет.

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_terminal_status_stamp.py -q`
Expected: PASS (3 параметра).

- [ ] **Step 6: Прогнать смежное**

Run: `uv run pytest tests/tui/test_terminal_status_stamp.py tests/tui/test_header_status_render.py tests/tui/test_agent_runner.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/tui/services/agent_runner.py src/tui/screens/main.py tests/tui/test_terminal_status_stamp.py
git commit -m "tui: стамп терминального статуса (completed/stopped/failed) в meta.tui.status"
```

---

## Финальная проверка

- [ ] Весь TUI-набор: `uv run pytest tests/tui -q` — без новых падений (учесть известный baseline пред-сущ. Windows-падений).
- [ ] (Ручное) Живой прогон: статус [running] золотистый во время работы; после завершения [completed] зелёный; после /stop [stopped] серый; при ошибке [failed] красный; индикатор `контекст …%` растёт и краснеет к 90%+; в responses-режиме индикатора нет.

---

## Self-Review

**Spec coverage:**
- #1 индикатор (размещение/формат/цвет/видимость/данные TUI-side) → T1 (хелпер+цвет) + T3 (данные live) + T4 (рендер в шапке). ✓
- #3 статусы (набор/цвета/деривация/персист в meta.tui.status/охват TUI) → T2 (цвет) + T4 (бейдж из meta.tui.status, рендер) + T5 (стамп исхода). ✓
- `compacting` не статус — не вводим. ✓ Server/swarm не трогаем — бейдж лишь читает meta. ✓

**Placeholder scan:** плейсхолдеров нет — весь код и команды готовы.

**Type consistency:** `render_context_meter(int,int)->tuple[str,str]|None` (T1) ← вызывается в `_context_meter` (T4). `status_badge_color(str)->str` (T2) ← в `_compose_header_segments` (T4) на результате `_task_status_badge()->str` (бейдж в скобках). `read_live_messages(task_id)->list[dict]` (T3) ← `_context_meter` (T4) → `calc_history_chars_for_trigger(list[dict])`. `stop_requested:bool` (T5) ← читается в `_poll_active` (T5). `update_tui_meta(task_id, {"status":...})` — существующий метод. Имена/типы согласованы.
