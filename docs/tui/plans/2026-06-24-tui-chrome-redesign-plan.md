# Редизайн хрома TUI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переразложить хром TUI — отдельная мета-строка над вводом (модель·контекст·статус), всегда-зарезервированный спиннер внизу трейса с состоянием «Выполнил за N», разгруженные хедер/футер, адаптивный путь, статус сессии в заголовке терминала — не ломая существующее.

**Architecture:** Презентационные изменения. Чистые хелперы (truncate_path, format_terminal_title) выносим и тестируем изолированно; виджет ThinkingIndicator получает состояние `done`; `main.py` compose/poll переразлагает полосы и проводит обновление заголовка/спиннера. Логика прогона/суммаризации/статусов/ask_human/confirm — не трогается.

**Tech Stack:** Python ≥3.10, Textual, pytest (+ pytest snapshot для TUI). Запуск тестов TUI — `uv run pytest tests/tui/...`.

Спека: `docs/tui/specs/2026-06-24-tui-chrome-redesign-design.md`.

## Global Constraints

- **Хирургические правки**: менять только требуемое спекой; не рефакторить попутно; каждая правка прослеживается к спеке.
- **Палитра только из токенов** `src/tui/palette.py` ↔ `src/tui/styles.tcss` (правим оба зеркально); новых цветов не вводим. Единственное изменение значения — имя модели → `$ink #ebe9e6`.
- **Поведение не трогаем**: суммаризация, цикл прогона, статусы/бейдж, ask_human, confirm-баннеры, слот footer-hint (clear/stop/paste), history-навигация, плашки вставки.
- **Снапшоты осознанно**: регенерировать так, чтобы diff содержал ТОЛЬКО намеренные изменения; неожиданные сдвиги — разбирать.
- **Регрессия зелёная**: до и после — `uv run pytest tests/tui` без новых падений (пред-существующие Windows-падения не маскировать).
- **Конфликт «спека ↔ существующее» → STOP**: подсветить, обсудить, согласовать; не ломать молча. Репортить как DONE_WITH_CONCERNS / BLOCKED.
- Абсолютные импорты от `src/`. Коммиты лаконичные RU/mixed; без упоминаний AI/Claude/Anthropic, без `Co-Authored-By`. Не пушить (контроллер пушит).
- Цвета-ориентир: deep-dim `#7a8290`, dim `#8b949e`, ink `#ebe9e6`, accent/teal `#54B3A6`, OK `#5FAE7F`, RUN/amber `#D4A574`, WARN `#f0883e`, ERR `#C75C54`.

---

### Task 1: `truncate_path` — адаптивный путь (чистая функция)

**Files:**
- Create: `src/tui/services/path_display.py`
- Test: `tests/tui/test_path_display.py`

**Interfaces:**
- Produces: `truncate_path(path: str, max_cols: int) -> str`

- [ ] **Step 1: Тест (RED)** — `tests/tui/test_path_display.py`:

```python
from tui.services.path_display import truncate_path


def test_full_path_when_fits():
    p = r"D:\dev\proj"
    assert truncate_path(p, 40) == p


def test_middle_collapsed_keeps_root_and_tail():
    p = r"D:\dev\PycharmProjects\ai\ReAct-Agent"
    out = truncate_path(p, 22)
    assert out.startswith("D:") and out.endswith("ReAct-Agent") and "…" in out
    assert len(out) <= 22


def test_very_narrow_keeps_tail_with_ellipsis():
    p = r"D:\dev\PycharmProjects\ai\ReAct-Agent"
    out = truncate_path(p, 15)
    assert out.endswith("ReAct-Agent")
    assert out.startswith("…")
    assert len(out) <= 15


def test_tail_longer_than_max_truncates_tail():
    p = r"D:\very-long-final-directory-name"
    out = truncate_path(p, 10)
    assert len(out) <= 10


def test_posix_separator():
    p = "/home/dmitry/work/project"
    out = truncate_path(p, 16)
    assert out.endswith("project") and len(out) <= 16
```

- [ ] **Step 2: RED** — `uv run pytest tests/tui/test_path_display.py -q` → FAIL (нет модуля).

- [ ] **Step 3: Реализация** — `src/tui/services/path_display.py`:

```python
"""Адаптивное отображение пути (cwd) в шапке TUI — чистая презентация.

Влезает целиком → весь путь; иначе сохраняем корень + последний сегмент, середину
схлопываем в `…`; совсем узко → `…<sep><last>`; хвост длиннее max — обрезаем хвост."""
from __future__ import annotations


def truncate_path(path: str, max_cols: int) -> str:
    if max_cols <= 0:
        return ""
    if len(path) <= max_cols:
        return path
    sep = "\\" if "\\" in path else "/"
    nonempty = [p for p in path.split(sep) if p]
    if not nonempty:
        return path[-max_cols:]
    tail = nonempty[-1]
    minimal = f"…{sep}{tail}"
    if len(minimal) > max_cols:
        # даже хвост не влезает — показать конец хвоста с ведущим …
        return ("…" + tail)[-max_cols:]
    # жадно наращиваем голову от корня, пока `<head><sep>…<sep><tail>` влезает
    best = minimal
    head_parts = nonempty[:-1]
    for k in range(1, len(head_parts) + 1):
        cand = f"{sep.join(head_parts[:k])}{sep}…{sep}{tail}"
        if len(cand) <= max_cols:
            best = cand
        else:
            break
    return best
```

- [ ] **Step 4: GREEN** — `uv run pytest tests/tui/test_path_display.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/path_display.py tests/tui/test_path_display.py
git commit -m "feat(tui): чистый хелпер truncate_path — адаптивный путь в шапке"
```

---

### Task 2: `format_terminal_title` со статусом (чистая функция)

**Files:**
- Modify: `src/tui/services/terminal_title.py`
- Test: `tests/tui/test_terminal_title.py` (создать, если нет)

**Interfaces:**
- Consumes: —
- Produces: `format_terminal_title(task: str | None = None, status: str | None = None, *, max_len: int = 50) -> str`

- [ ] **Step 1: Тест (RED)** — добавить в `tests/tui/test_terminal_title.py` (если файла нет — создать с импортом):

```python
from tui.services.terminal_title import format_terminal_title


def test_base_no_task_no_status():
    assert format_terminal_title() == "✦ GigaCode"


def test_status_without_task():
    assert format_terminal_title(status="[running]") == "✦ GigaCode · [running]"


def test_status_before_prompt():
    out = format_terminal_title("сделай рефактор", status="[running]")
    assert out == "✦ GigaCode · [running] · сделай рефактор"


def test_base_and_status_always_kept_prompt_truncated():
    long = "x" * 200
    out = format_terminal_title(long, status="[running]", max_len=10)
    assert out.startswith("✦ GigaCode · [running] · ")
    assert out.endswith("…")


def test_backward_compatible_no_status():
    assert format_terminal_title("привет") == "✦ GigaCode · привет"
```

- [ ] **Step 2: RED** — `uv run pytest tests/tui/test_terminal_title.py -q` → FAIL (нет параметра status / нового поведения).

- [ ] **Step 3: Реализация** — заменить `format_terminal_title` в `src/tui/services/terminal_title.py`:

```python
def format_terminal_title(
    task: Optional[str] = None,
    status: Optional[str] = None,
    *,
    max_len: int = 50,
) -> str:
    base = f"{_EMBLEM} {_APP_NAME}"
    if status:
        base = f"{base} · {status}"
    one_line = " ".join(task.split()) if task else ""
    if not one_line:
        return base
    if len(one_line) > max_len:
        one_line = one_line[:max_len] + "…"
    return f"{base} · {one_line}"
```

`set_terminal_title` не трогаем.

- [ ] **Step 4: GREEN** — `uv run pytest tests/tui/test_terminal_title.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/terminal_title.py tests/tui/test_terminal_title.py
git commit -m "feat(tui): заголовок терминала — статус с приоритетом (промпт ужимается)"
```

---

### Task 3: Проводка заголовка — живой статус + сброс на /new (фикс бага)

**Files:**
- Modify: `src/tui/screens/main.py` (`_on_user_text_submitted:651`, falling-edge `~1269-1280`, `_cmd_new:2109`)
- Test: `tests/tui/test_terminal_title_wiring.py` (создать)

**Interfaces:**
- Consumes: `format_terminal_title(task, status)`, `set_terminal_title(text)`, `_task_status_badge()`.
- Produces: заголовок отражает текущий статус; на `/new` — база `✦ GigaCode`.

- [ ] **Step 1: Тест (RED)** — `tests/tui/test_terminal_title_wiring.py` (репро бага /new + статус):

```python
import pytest


@pytest.mark.asyncio
async def test_new_resets_terminal_title(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    import tui.services.terminal_title as tt
    from tui.app import TuiApp

    calls = []
    monkeypatch.setattr(tt, "set_terminal_title", lambda text, **k: calls.append(text))
    # main.py импортирует символы из модуля внутри функций — патчим там же:
    import tui.screens.main as main_mod
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._on_user_text_submitted("какой-то предыдущий промпт")
        await pilot.pause()
        calls.clear()
        scr._cmd_new()
        await pilot.pause()
        assert any(c == "✦ GigaCode" for c in calls), f"ожидали сброс заголовка, calls={calls}"
```

ВНИМАНИЕ имплементеру: `main.py` импортирует `set_terminal_title`/`format_terminal_title` локально внутри методов (`from tui.services.terminal_title import ...`). Чтобы тест мог перехватить вызов, в Task 3 **подними импорт на уровень модуля** `tui/screens/main.py` (один раз, вверху: `from tui.services.terminal_title import format_terminal_title, set_terminal_title`) и убери локальные импорты в `_on_user_text_submitted`/`_update_terminal_title`. Это и есть точка патча в тесте (`main_mod.set_terminal_title`).

- [ ] **Step 2: RED** — `uv run pytest tests/tui/test_terminal_title_wiring.py -q` → FAIL (на /new заголовок не сбрасывается).

- [ ] **Step 3: Реализация**:
  1. Вверху `main.py` добавить модульный импорт: `from tui.services.terminal_title import format_terminal_title, set_terminal_title`; убрать локальные импорты в `_on_user_text_submitted` (655) и в `main.py:655-656` оставить вызов.
  2. Добавить хелпер:

```python
def _update_terminal_title(self) -> None:
    """Заголовок терминала = последний промпт + текущий статус (для таба/таскбара)."""
    status = self._task_status_badge() if self._active_task_id is not None else None
    prompt = self._last_user_send_text if self._active_task_id is not None else None
    set_terminal_title(format_terminal_title(prompt, status))
```

  3. В `_on_user_text_submitted` заменить `set_terminal_title(format_terminal_title(text))` на установку с running-статусом после сохранения `self._last_user_send_text = text`: вызвать `self._update_terminal_title()`.
  4. В falling-edge (после вычисления `outcome` и `update_tui_meta({"status": outcome})`, ~1279) добавить `self._update_terminal_title()` — чтобы таб показал `[completed]/[stopped]/[failed]`.
  5. В `_cmd_new` (2109) в начале (после входа) добавить `set_terminal_title(format_terminal_title())` — сброс на базу. (Также безопасно продублировать в `_reset_render_state` — но достаточно `_cmd_new`; не плодить вызовы без нужды.)

- [ ] **Step 4: GREEN** — `uv run pytest tests/tui/test_terminal_title_wiring.py -q` → PASS. Затем регрессия онбординга заголовка: `uv run pytest tests/tui/test_terminal_title.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_terminal_title_wiring.py
git commit -m "fix(tui): заголовок терминала отражает статус и сбрасывается на /new"
```

---

### Task 4: ThinkingIndicator — состояние `done` («✓ Выполнил за N», серым, замороженное)

**Files:**
- Modify: `src/tui/widgets/thinking_indicator.py`
- Test: `tests/tui/test_thinking_indicator.py` (добавить кейсы; файл существует)

**Interfaces:**
- Consumes: —
- Produces: `ThinkingIndicator.set_state("done")` → рендер `✓ Выполнил за <duration>` цветом `_DIM` (deep-dim), длительность заморожена на момент перехода.

- [ ] **Step 1: Тест (RED)** — добавить в `tests/tui/test_thinking_indicator.py`:

```python
def test_done_state_renders_vypolnil_dim_and_frozen():
    import tui.widgets.thinking_indicator as ti
    clock = {"t": 100.0}
    ind = ti.ThinkingIndicator(started_at=0.0, clock=lambda: clock["t"])
    ind.set_state("done")          # заморозить на 100с
    clock["t"] = 999.0             # время идёт дальше
    txt = ind._render_text()
    s = txt.plain
    assert s.startswith("✓") and "Выполнил за" in s
    # длительность заморожена (≈100с от started_at), не 999
    assert "1м 40с" in s or "100" in s
```
(формат длительности — через `format_duration`; проверяем, что взято замороженное значение, а не текущее.)

- [ ] **Step 2: RED** — `uv run pytest tests/tui/test_thinking_indicator.py -q -k done` → FAIL.

- [ ] **Step 3: Реализация** в `thinking_indicator.py`:
  1. Добавить константу `_STATE_DONE = "done"`.
  2. В `__init__` добавить `self._frozen_elapsed: int | None = None`.
  3. В `set_state` расширить whitelist и заморозить:

```python
def set_state(self, state: str) -> None:
    if state not in (_STATE_THINKING, _STATE_VALIDATING, _STATE_COMPRESSING, _STATE_PROCESSING, _STATE_DONE):
        return
    if self._state == state:
        return
    self._state = state
    if state == _STATE_DONE:
        self._frozen_elapsed = int(self._clock() - self._started_at)
    if self.is_mounted:
        self.refresh()
```

  4. В `_render_text`, ПЕРЕД блоком `elapsed = int(...)`, добавить ранний возврат для done:

```python
        if self._state == _STATE_DONE:
            secs = self._frozen_elapsed if self._frozen_elapsed is not None \
                else int(self._clock() - self._started_at)
            text.append("✓", style=_DIM)
            text.append(" ")
            text.append(f"Выполнил за {format_duration(secs)}", style=_DIM)
            return text
```
(вставить как первую ветку в цепочке if/elif состояний, чтобы done не падал в общий `else`-thinking.)

- [ ] **Step 4: GREEN** — `uv run pytest tests/tui/test_thinking_indicator.py -q` → PASS (все, включая существующие).

- [ ] **Step 5: Commit**

```bash
git add src/tui/widgets/thinking_indicator.py tests/tui/test_thinking_indicator.py
git commit -m "feat(tui): ThinkingIndicator — состояние done «✓ Выполнил за N» (серым, заморожено)"
```

---

### Task 5: Персистентность спиннера — на успешном завершении переключать в `done`, не уничтожать

**Files:**
- Modify: `src/tui/screens/main.py` (poll else-branch `~1372`)
- Test: `tests/tui/test_thinking_persist.py` (создать)

**Interfaces:**
- Consumes: `ThinkingIndicator.set_state("done")`, `_task_status_badge()`, `_destroy_thinking_indicator()`.
- Produces: после успешного хода спиннер остаётся в `done`; на stopped/failed/fresh — уничтожается (как сейчас); на новом user-send — сбрасывается (через `_reset_thinking_timer`/`_reset_render_state`, существующий путь).

- [ ] **Step 1: Тест (RED)** — `tests/tui/test_thinking_persist.py`:

```python
import pytest


@pytest.mark.asyncio
async def test_indicator_persists_done_after_completed(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.thinking_indicator import ThinkingIndicator

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("t-1")
        await pilot.pause()
        # смонтировать индикатор (как во время прогона) и пометить задачу completed
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        scr._store.update_tui_meta("t-1", {"status": "completed"})
        # эмуляция idle-тика poll: not running → ветка done-persist
        scr._poll_active()
        await pilot.pause()
        ind = scr._thinking
        assert ind is not None, "индикатор не должен уничтожаться на completed"
        assert ind._state == "done"
```
(Имена `_show_thinking_indicator`/`_thinking`/`_poll_active` — существующие. Если `_poll_active` требует активного runner — имплементеру адаптировать сетап под реальный контракт метода; цель теста: на idle-тике после completed индикатор жив и в done.)

- [ ] **Step 2: RED** — `uv run pytest tests/tui/test_thinking_persist.py -q` → FAIL (сейчас else-ветка уничтожает).

- [ ] **Step 3: Реализация** — в `_poll_active`, ветка `else:` (сейчас `self._destroy_thinking_indicator(); self._clear_stream_block()`, ~1372-1374) заменить на:

```python
        else:
            # Idle. Если ход успешно завершён — спиннер ОСТАЁТСЯ как «✓ Выполнил за N»
            # (персистентно до следующего user-send, который сбросит его через
            # _reset_thinking_timer). На stopped/failed/fresh — уничтожаем, как раньше.
            outcome = None
            if self._store is not None and self._active_task_id is not None:
                outcome = (self._store.get_task_meta(self._active_task_id).get("tui") or {}).get("status")
            if outcome == "completed" and self._thinking is not None:
                self._thinking.set_state("done")
            else:
                self._destroy_thinking_indicator()
            self._clear_stream_block()
```

- [ ] **Step 4: GREEN** — `uv run pytest tests/tui/test_thinking_persist.py -q` → PASS. Регрессия индикатора/статусов: `uv run pytest tests/tui -q -k "thinking or terminal_status or stop_pending or header"` → без новых падений.

- [ ] **Step 5: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_thinking_persist.py
git commit -m "feat(tui): спиннер персистентен после успешного хода (done «Выполнил за N»)"
```

---

### Task 6: Реструктуризация хрома — мета-строка, хедер/футер, разделители, адаптивный путь

**Files:**
- Modify: `src/tui/branding.py` (welcome-строка)
- Modify: `src/tui/styles.tcss` (футер h2, `#meta`-блок, разделители)
- Modify: `src/tui/screens/main.py` (`compose:161`, `_compose_header_segments:177`, новый `_compose_meta_segments`, `on_resize`)
- Test: `tests/tui/test_meta_and_header.py` (создать) + регенерация снапшотов

**Interfaces:**
- Consumes: `truncate_path` (Task 1), `render_context_meter`, `status_badge_color`, `_task_status_badge`, `_context_meter`.
- Produces: новая полоса `#meta` (модель·контекст слева, статус справа); хедер-слева = `Сессия: <id>`/welcome; хедер-справа = адаптивный cwd; футер h2 (команды слева, gc-ua справа).

Эта задача — интеграционная (CSS + compose + branding + снапшоты вместе, чтобы не оставлять промежуточно-сломанного экрана). Соблюдать Global Constraints буквально: при любом конфликте с существующим — STOP и в отчёт.

- [ ] **Step 1: Тест (RED)** — `tests/tui/test_meta_and_header.py` (чистая проверка композиции + наличие виджетов):

```python
import pytest


@pytest.mark.asyncio
async def test_header_left_welcome_when_fresh(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        left, _right = scr._compose_header_segments()
        s = left if isinstance(left, str) else left.plain
        assert "Добро пожаловать в GigaCode" in s
        assert "модель" not in s and "контекст" not in s   # уехало в мету


@pytest.mark.asyncio
async def test_header_left_session_when_active(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("abcd1234efgh")
        await pilot.pause()
        left, _ = scr._compose_header_segments()
        s = left if isinstance(left, str) else left.plain
        assert "Сессия:" in s and "abcd1234" in s


@pytest.mark.asyncio
async def test_meta_has_model_context_and_status(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task("abcd1234")
        await pilot.pause()
        from textual.widgets import Static
        meta_left = scr.query_one("#meta-left", Static)
        assert meta_left is not None   # мета-полоса существует
```

- [ ] **Step 2: RED** — `uv run pytest tests/tui/test_meta_and_header.py -q` → FAIL (нет welcome / нет `#meta-left`).

- [ ] **Step 3: branding.py** — добавить welcome-строку, не ломая `HEADER_RIGHT` (его импортируют другие экраны):

```python
WELCOME = "Добро пожаловать в GigaCode"
```
`HEADER_RIGHT`/`HEADER_LEFT_EMPTY` НЕ удалять (используются в skills/mcp/config/help экранах) — главный экран просто перестаёт их использовать.

- [ ] **Step 4: styles.tcss** — зеркально палитре; добавить/изменить (значения-ориентир, при снапшот-сдвигах тюнить, в рамках принятых правил):

```css
/* Мета-полоса над вводом: модель·контекст слева, статус справа */
#meta { height: 1; background: $bg; color: $deep-dim; padding: 0 1; border-bottom: solid $deep-dim; }
#meta-left  { width: 1fr; color: $deep-dim; }
#meta-right { width: auto; color: $deep-dim; text-align: right; padding-right: 1; }

/* Разделители между полосами (как у хедер/футер) */
#trace { border-bottom: solid $deep-dim; }
#input-row { border-bottom: solid $deep-dim; }

/* Футер на 2 строки: текст сверху, пустая снизу (ввод не липнет к низу) */
#footer { height: 2; }
```
(`#header`/`#footer` уже имеют `border-bottom`/фон — не дублировать. Спиннер — часть `#trace`, отдельной линии над ним НЕ добавлять.)

- [ ] **Step 5: main.py — compose + header/meta split + on_resize**:
  1. В `compose` (161) вставить мета-полосу МЕЖДУ `#trace` и `#input-row`:

```python
            yield VerticalScroll(id="trace")
            with Horizontal(id="meta"):
                ml, mr = self._compose_meta_segments()
                yield Static(ml, id="meta-left", markup=False)
                yield Static(mr, id="meta-right", markup=False)
            with Horizontal(id="input-row"):
                ...
```

  2. Переписать `_compose_header_segments` — хедер БОЛЬШЕ не несёт модель/контекст/статус: слева `Сессия: <id8>` или `WELCOME`; справа — адаптивный cwd:

```python
    def _compose_header_segments(self) -> "tuple[Text | str, str]":
        from tui.branding import WELCOME
        if self._active_task_id is None:
            left = WELCOME
        else:
            left = f"Сессия: {self._active_task_id[:8]}"
        right = truncate_path(self._current_cwd(), self._header_right_cols())
        return left, right
```
(`truncate_path` — `from tui.services.path_display import truncate_path` вверху модуля. `_current_cwd()` существует (289). `_header_right_cols()` — см. ниже.)

  3. Новый `_compose_meta_segments` — несёт модель (ink) · контекст · статус:

```python
    def _compose_meta_segments(self) -> "tuple[Text, Text]":
        from tui.palette import INK
        cfg = getattr(self.app, "_config", None) or {}
        model = cfg.get("model", "?")
        left = Text()
        left.append(str(model), style=INK)
        meter = self._context_meter()
        if meter is not None:
            text, color = meter
            left.append("  ·  ", style=_DIM)
            left.append(text, style=color)
        right = Text()
        if self._active_task_id is not None:
            status = self._task_status_badge()
            right.append(status, style=status_badge_color(status))
        else:
            right.append("[—]", style=_DIM)
        return left, right
```
(`INK` — добавить в импорт палитры рядом с существующими. `_context_meter`/`status_badge_color`/`_task_status_badge` — существующие. Свежий экран: meter обычно None или 0% — `[—]` справа.)

  4. Добавить `_refresh_meta` (по аналогии с `_refresh_header`) и звать его там же, где сейчас `_refresh_header` (poll-тик):

```python
    def _refresh_meta(self) -> None:
        ml, mr = self._compose_meta_segments()
        try:
            self.query_one("#meta-left", Static).update(ml)
            self.query_one("#meta-right", Static).update(mr)
        except Exception:
            pass
```
Вызвать `self._refresh_meta()` рядом с каждым `self._refresh_header()` (поиск по файлу). `_refresh_header` теперь обновляет только сессию/путь.

  5. Ширина для усечения пути + пересчёт на ресайз:

```python
    def _header_right_cols(self) -> int:
        # доступно правому слоту: ширина экрана минус левый текст и поля. Берём
        # консервативно ~половину ширины терминала (левый слот — Сессия:/welcome).
        try:
            w = self.app.size.width
        except Exception:
            w = 80
        return max(12, w - 34)

    def on_resize(self, event) -> None:
        self._refresh_header()   # пересчитать усечение пути под новую ширину
```
(Если в классе уже есть `on_resize` — дописать в него вызов, не плодить второй.)

- [ ] **Step 6: GREEN (юнит)** — `uv run pytest tests/tui/test_meta_and_header.py -q` → PASS.

- [ ] **Step 7: Снапшоты** — прогнать снапшот-тесты главного экрана; они упадут из-за намеренной реструктуризации. Просмотреть diff каждого (мета-полоса, футер h2, разделители, хедер без модели) — убедиться, что изменения ТОЛЬКО намеренные, затем регенерировать (`uv run pytest tests/tui -q -k snapshot --snapshot-update`). Если в diff есть что-то НЕ из спеки — STOP, разобрать.

- [ ] **Step 8: Регрессия** — `uv run pytest tests/tui -q` → без новых падений против базлайна (пред-существующие Windows-падения — сверить, что те же). Особое внимание: footer-hint (clear/stop/paste) слот работает; ask_human; context-meter в мете.

- [ ] **Step 9: Commit**

```bash
git add src/tui/branding.py src/tui/styles.tcss src/tui/screens/main.py tests/tui/test_meta_and_header.py tests/tui/__snapshots__
git commit -m "feat(tui): хром — мета-строка над вводом, адаптивный путь, футер h2, разделители полос"
```

---

## Self-Review

**1. Spec coverage:**
- Раскладка (мета над вводом, разделители, футер h2) → Task 6. ✓
- Хедер (Сессия:/welcome, адаптивный путь, дроп модель/chat/статус/F1) → Task 6 + Task 1 (truncate). ✓
- Спиннер: done «Выполнил за N» серым + персистентность + свежий без него → Task 4 (done-state) + Task 5 (персистентность; свежий — индикатор None, не создаётся). ✓
- Мета (модель ink · контекст как сейчас · статус справа, `[—]` свежий) → Task 6. ✓
- Подсказки footer-left без изменений → не трогаем (Global Constraints; Task 6 Step 8 регрессия). ✓
- Заголовок терминала со статусом + фикс /new → Task 2 (формат) + Task 3 (проводка). ✓
- Цвета из палитры, имя модели → ink → Task 6 (INK). ✓
- «Не ломать существующее» → Global Constraints + Task 6 Step 7/8. ✓

**2. Placeholder scan:** код приведён для новых функций/виджета/CSS/compose; команды и ожидаемые результаты указаны. Места проводки названы по символам и строкам. Нет «TODO/аналогично Task N».

**3. Type consistency:** `truncate_path(path, max_cols)`, `format_terminal_title(task, status, *, max_len)`, `set_state("done")`/`_frozen_elapsed`, `_compose_meta_segments() -> (Text, Text)`, `_compose_header_segments() -> (str, str)` (теперь оба str — путь строка), `_refresh_meta`, `_header_right_cols` — имена согласованы между задачами и тестами.

**Открытые нюансы (для имплементера, в отчёт DONE_WITH_CONCERNS если всплывут):**
- `_compose_header_segments` теперь возвращает `(str, str)` (раньше мог `Text`); вызовы (`_refresh_header`, `compose`) принимают и str, и Text (Static.update оба ок) — проверить.
- Точная ширина `_header_right_cols` (минус левый слот) — подбирается визуально; стартовое `w-34` тюнить по снапшотам.
- Форма статуса в заголовке = англ. `[running]` (как бейдж); если попросят русские — отдельная правка `_task_status_badge`-маппинга только для заголовка.
