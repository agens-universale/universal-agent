# TUI Visual Unification (Rounded Frames) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Привести визуальный слой TUI к единому языку: скруглённые рамки (`border: round`) для всех «живых» блоков, стартовый экран как teal-карточка (логотип + MCP), отделить защитные шлюзы глифом `⚠`, отвеченные вопросы — в аккуратной рамке.

**Architecture:** Изменения сосредоточены в презентационном слое `src/tui/`. Большинство — точечные правки CSS (`styles.tcss`) + синхронизация токенов (`palette.py`). Логика затрагивается в `confirm_banner.py` (глиф шлюза), `trace_render.py` + `screens/main.py` (общий хелпер отвеченного вопроса), `screens/main.py` + `branding.py` (стартовая карточка A2). Проверка — юнит-тесты для чистых хелперов + перегенерация SVG-снапшотов.

**Tech Stack:** Python ≥3.10, Textual (TUI), Rich (`rich.text.Text`), pytest + pytest-textual-snapshot. Тесты гоняются через `uv run pytest` (TUI-extra только в `.venv/`).

## Global Constraints

- **Цвет-токены дублируются в двух файлах:** `src/tui/styles.tcss` (`$vars`) и `src/tui/palette.py` (константы). Любое добавление/правка токена — **синхронно в оба**; имена констант зеркалят `$vars` (`$frame-archive` → `FRAME_ARCHIVE`).
- **Абсолютные импорты от `src/`** (`from tui.palette import ...`), не относительные.
- **Коммиты:** лаконично, по-русски/смешанно как в окружении; **без упоминаний AI/Claude/Anthropic**, без `Co-Authored-By`.
- **Снапшоты не перегенерировать вслепую:** после `--snapshot-update` открыть SVG и глазами убедиться, что вид соответствует задуманному, только потом коммитить.
- **Не трогать:** поле ввода (`#cmdinput`, `TextArea.ask-human-mode`), геометрию хедера, одиночные линии-разделители и `border-left`-рельсы, таблицы `markdown_render` (`_box.SQUARE`).
- **TUI-тесты:** `uv run pytest tests/tui/...`; снапшоты — `uv run pytest <file> --snapshot-update`.

---

## File Structure

- `src/tui/styles.tcss` — все CSS-правки (рамки баннеров, палитры, конфиг-панели, стартовая карточка, новый класс `.answered-question`, новый токен `$frame-archive`).
- `src/tui/palette.py` — новый токен `FRAME_ARCHIVE` (зеркало `$frame-archive`).
- `src/tui/widgets/confirm_banner.py` — глиф шлюза `⚠` (хелпер `build_guard_label`).
- `src/tui/services/trace_render.py` — общий хелпер `build_answered_question_widget`; замена readonly-рендера ask_human.
- `src/tui/screens/main.py` — live-путь ask_human (`_on_ask_human_event`) через общий хелпер; стартовая карточка (`_show_brand`, `_update_mcp_indicator`, `_settle_mcp_row`, `_TRACE_HEADER_IDS`).
- `src/tui/branding.py` — без структурных изменений (тэглайн переиспользуется как subtitle).
- Тесты: `tests/tui/test_branding.py`, `tests/tui/test_confirm_banner_label.py` (новый), `tests/tui/test_answered_question.py` (новый), снапшоты `test_snapshot_ask_human_banner`, `test_snapshot_confirm_banner`, `test_snapshot_palette_open`, `test_snapshot_main_full_dialog`, `test_snapshot_empty_state`, `test_mcp_boot_snapshot`, `test_mcp_snapshot`.

Порядок задач: от изолированных CSS-правок (низкий риск) к стартовой карточке A2 (высокий). Каждая задача — самостоятельный коммит.

---

### Task 1: Баннер ask_human → скруглённая рамка

**Files:**
- Modify: `src/tui/styles.tcss` (`.ask-human-banner`, ~стр. 238-246)
- Test: `tests/tui/test_snapshot_ask_human_banner.py` (перегенерация)

**Interfaces:**
- Consumes: ничего.
- Produces: ничего (чистый CSS).

- [ ] **Step 1: Заменить рамку на `round`**

В `src/tui/styles.tcss`, правило `.ask-human-banner` — убрать `border-left: thick` и сменить `solid`→`round`:

```css
.ask-human-banner {
    background: #161A23;
    border: round $run-color;
    padding: 0 1;
    margin: 0 1;
    height: auto;
    width: 1fr;
}
```

(Строку `border-left: thick $run-color;` удалить полностью.)

- [ ] **Step 2: Перегенерировать снапшот**

Run: `uv run pytest tests/tui/test_snapshot_ask_human_banner.py --snapshot-update`
Expected: 1 passed; обновлены `.raw`/`.svg`.

- [ ] **Step 3: Глазами проверить SVG**

Открыть `tests/tui/__snapshots__/test_snapshot_ask_human_banner/test_ask_human_banner_snapshot.svg`.
Expected: ровная скруглённая янтарная рамка по периметру, без толстого левого края, углы скруглены.

- [ ] **Step 4: Прогнать тест без апдейта (зелёный)**

Run: `uv run pytest tests/tui/test_snapshot_ask_human_banner.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/styles.tcss tests/tui/__snapshots__/test_snapshot_ask_human_banner
git commit -m "feat(tui): скруглить рамку баннера ask_human, убрать толстый левый край"
```

---

### Task 2: Баннер confirm → скруглённая рамка + глиф шлюза «⚠»

**Files:**
- Modify: `src/tui/styles.tcss` (`.confirm-banner`, ~стр. 253-261)
- Modify: `src/tui/widgets/confirm_banner.py` (импорты, новый `build_guard_label`, `compose`)
- Create: `tests/tui/test_confirm_banner_label.py`
- Test: `tests/tui/test_snapshot_confirm_banner.py` (перегенерация)

**Interfaces:**
- Consumes: `tui.palette.RUN_COLOR`, `tui.palette.BRIGHT`.
- Produces: `confirm_banner.GUARD_GLYPH: str`, `confirm_banner.build_guard_label(title: str) -> rich.text.Text`.

- [ ] **Step 1: Failing-тест на хелпер метки шлюза**

Create `tests/tui/test_confirm_banner_label.py`:

```python
"""Метка-заголовок confirm/guard баннера — глиф шлюза вместо '?'."""
from tui.widgets.confirm_banner import GUARD_GLYPH, build_guard_label
from tui.palette import RUN_COLOR


def test_guard_label_uses_caution_glyph_not_question():
    label = build_guard_label("Защита записи")
    assert label.plain == f"{GUARD_GLYPH} Защита записи"
    assert "?" not in label.plain


def test_guard_glyph_is_amber():
    label = build_guard_label("Защита чтения")
    # первый span красит глиф в run-color (янтарь)
    first = label.spans[0]
    assert first.start == 0
    assert str(first.style) == RUN_COLOR
```

- [ ] **Step 2: Запустить — упадёт (нет символов)**

Run: `uv run pytest tests/tui/test_confirm_banner_label.py -q`
Expected: FAIL (ImportError: cannot import name 'GUARD_GLYPH' / 'build_guard_label').

- [ ] **Step 3: Реализовать хелпер и применить в `compose`**

В `src/tui/widgets/confirm_banner.py`:

Добавить к импортам `RUN_COLOR`, `BRIGHT`:

```python
from tui.palette import ACCENT, BG, BRIGHT, DIM, RUN_COLOR
```

Добавить после импортов (рядом с `_OPTIONS`):

```python
# Глиф защитного шлюза (caution). НЕ "?" — это не вопрос агента, а security-gate.
# Width-safe-кандидат; если ⚠ в целевом терминале рендерится шире 1 ячейки —
# заменить на "▲" или "!" (единственная точка правки).
GUARD_GLYPH = "⚠"


def build_guard_label(title: str) -> "Text":
    """Заголовок шлюза: янтарный глиф ⚠ + bright-титул. '?' остаётся только
    в самой строке вопроса ('Разрешить …?'), не в заголовке."""
    t = Text()
    t.append(f"{GUARD_GLYPH} ", style=RUN_COLOR)
    t.append(title, style=BRIGHT)
    return t
```

В `compose` заменить строку метки:

```python
        yield Static(build_guard_label(self.banner_title), classes="cb-label", markup=False)
```

(была: `yield Static(f"? {self.banner_title}", classes="cb-label", markup=False)`.)

- [ ] **Step 4: Тест на хелпер — зелёный**

Run: `uv run pytest tests/tui/test_confirm_banner_label.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Скруглить рамку confirm**

В `src/tui/styles.tcss`, правило `.confirm-banner` — убрать `border-left: thick`, `solid`→`round`:

```css
.confirm-banner {
    background: #161A23;
    border: round $accent;
    padding: 0 1;
    margin: 0 1;
    height: auto;
    width: 1fr;
}
```

- [ ] **Step 6: Перегенерировать и проверить снапшот**

Run: `uv run pytest tests/tui/test_snapshot_confirm_banner.py --snapshot-update`
Затем открыть `tests/tui/__snapshots__/test_snapshot_confirm_banner/test_confirm_banner_snapshot.svg`.
Expected: ровная скруглённая teal-рамка; заголовок начинается с янтарного `⚠`, без `?`; глиф занимает 1 ячейку (если визуально «разъехался» — заменить `GUARD_GLYPH` на `"▲"` и повторить Step 6).

- [ ] **Step 7: Прогон без апдейта**

Run: `uv run pytest tests/tui/test_snapshot_confirm_banner.py tests/tui/test_confirm_banner_label.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/tui/styles.tcss src/tui/widgets/confirm_banner.py tests/tui/test_confirm_banner_label.py tests/tui/__snapshots__/test_snapshot_confirm_banner
git commit -m "feat(tui): скруглить confirm-баннер и пометить шлюзы глифом ⚠ вместо ?"
```

---

### Task 3: Меню-оверлеи и панели конфига → скруглённые рамки

**Files:**
- Modify: `src/tui/styles.tcss` (`.palette` ~стр. 220, `.config-panel` ~стр. 304-311)
- Test: `tests/tui/test_snapshot_palette_open.py` (перегенерация)

**Interfaces:**
- Consumes: ничего.
- Produces: ничего (чистый CSS). Класс `.palette` шарят 4 виджета: `CommandPalette`, `SkillPicker`, `ArgPicker`, `FilePicker`.

- [ ] **Step 1: Скруглить `.palette`**

В `src/tui/styles.tcss`:

```css
.palette       { background: #161A23; border: round $run-color; max-height: 80%; }
```

(была `border: solid $run-color`.)

- [ ] **Step 2: Скруглить `.config-panel`**

В `src/tui/styles.tcss`, правило `.config-panel` — сменить только бордер:

```css
.config-panel {
    height: auto;
    border: round $deep-dim;
    border-title-color: $dim;
    border-subtitle-color: $deep-dim;
    padding: 0 1;
    margin: 0 0 1 0;
}
```

- [ ] **Step 3: Перегенерировать и проверить снапшот палитры**

Run: `uv run pytest tests/tui/test_snapshot_palette_open.py --snapshot-update`
Открыть `tests/tui/__snapshots__/test_snapshot_palette_open/test_palette_open_snapshot.svg`.
Expected: палитра команд в скруглённой янтарной рамке.

- [ ] **Step 4: Прогон без апдейта**

Run: `uv run pytest tests/tui/test_snapshot_palette_open.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/styles.tcss tests/tui/__snapshots__/test_snapshot_palette_open
git commit -m "feat(tui): скруглить рамки палитры/пикеров и панелей конфига"
```

---

### Task 4: Отвеченный вопрос → серая скруглённая рамка + янтарный «?»

**Files:**
- Modify: `src/tui/palette.py` (новый `FRAME_ARCHIVE`)
- Modify: `src/tui/styles.tcss` (новый токен `$frame-archive`, класс `.answered-question`)
- Modify: `src/tui/services/trace_render.py` (новый `build_answered_question_widget`; замена в `_dispatch_readonly`)
- Modify: `src/tui/screens/main.py` (`_on_ask_human_event` — через общий хелпер)
- Create: `tests/tui/test_answered_question.py`
- Test: `tests/tui/test_snapshot_main_full_dialog.py` (перегенерация)

**Interfaces:**
- Consumes: `tui.palette.RUN_COLOR`, `tui.palette.DIM`.
- Produces: `trace_render.build_answered_question_widget(question: str, *, event_no: int = 0, extra_classes: str = "") -> TraceRow`.

- [ ] **Step 1: Failing-тест на хелпер**

Create `tests/tui/test_answered_question.py`:

```python
"""Отвеченный вопрос ask_human в трейсе — янтарный '?' + текст, без 'ask_human:'."""
from tui.services.trace_render import build_answered_question_widget
from tui.palette import RUN_COLOR


def test_no_ask_human_prefix_amber_marker():
    w = build_answered_question_widget("Папка вне периметра. Создать там?", event_no=7)
    # content — rich.Text: "? <вопрос>" без технического префикса
    assert w._content.plain == "? Папка вне периметра. Создать там?"
    assert "ask_human:" not in w._content.plain
    # маркер '?' янтарный
    assert str(w._content.spans[0].style) == RUN_COLOR


def test_extra_classes_applied():
    w = build_answered_question_widget("Q", event_no=1, extra_classes="ask-human-question hidden")
    assert w.has_class("answered-question")
    assert w.has_class("ask-human-question")
    assert w.has_class("hidden")
    assert w._content.plain == "? Q"
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_answered_question.py -q`
Expected: FAIL (ImportError: cannot import name 'build_answered_question_widget').

- [ ] **Step 3: Добавить токен `FRAME_ARCHIVE` в оба файла**

В `src/tui/palette.py`, в секцию «Поверхности» (рядом с `BAND_BG`):

```python
FRAME_ARCHIVE = "#4b525d"  # серая рамка «архив/история» (отвеченный вопрос)
```

В `src/tui/styles.tcss`, рядом с `$band-bg`/`$focus-bg`:

```css
$frame-archive: #4b525d;   /* серая рамка «архив» — отвеченный вопрос */
```

- [ ] **Step 4: Реализовать хелпер в `trace_render.py`**

В `src/tui/services/trace_render.py` добавить функцию (рядом с `build_block_widgets`):

```python
def build_answered_question_widget(question, *, event_no: int = 0,
                                   extra_classes: str = ""):
    """Отвеченный вопрос ask_human как строка истории: серая round-рамка
    (CSS .answered-question) + янтарный маркер '?'. Тех-префикс 'ask_human:'
    не добавляем. Общий для live (main) и readonly (preview) путей.

    Цвет рамки — токен $frame-archive (серый). Альтернатива (живое сравнение
    после релиза) — янтарная рамка: сменить border в .answered-question на
    $run-color, точка правки одна."""
    from rich.text import Text
    from tui.widgets.trace_row import TraceRow
    from tui.palette import RUN_COLOR, DIM
    content = Text()
    content.append("? ", style=RUN_COLOR)
    content.append(question, style=DIM)
    cls = ("answered-question " + extra_classes).strip()
    return TraceRow(prefix="", content=content, kind_class=cls,
                    event_no=event_no, focusable=False)
```

- [ ] **Step 5: Тест на хелпер — зелёный**

Run: `uv run pytest tests/tui/test_answered_question.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Добавить CSS-класс `.answered-question`**

В `src/tui/styles.tcss` (рядом с `.ask-human-question.hidden`, ~стр. 166):

```css
.answered-question { border: round $frame-archive; color: $dim; padding: 0 1; margin: 1 0; width: 1fr; }
.answered-question.hidden { display: none; }
```

(Существующее правило `.ask-human-question.hidden { display: none; }` можно оставить — оно безвредно дублируется новым.)

- [ ] **Step 7: Заменить readonly-рендер ask_human**

В `src/tui/services/trace_render.py`, в `_dispatch_readonly`, ветка `elif ev.kind == "ask_human":` (стр. ~206-208) — заменить на:

```python
    elif ev.kind == "ask_human":
        mount_target.mount(build_answered_question_widget(ev.summary, event_no=ev.no))
```

- [ ] **Step 8: Заменить live-рендер ask_human в main.py**

В `src/tui/screens/main.py`, `_on_ask_human_event`, блок монтирования `self._ask_human_question_row = self._mount_trace_row(prefix="  ? ", content=f"ask_human: {ev.summary}", kind_class="line-meta ask-human-question hidden", event_no=ev.no)` — заменить на построение через общий хелпер с теми же классами для hidden→reveal-механики:

```python
        from tui.services.trace_render import build_answered_question_widget
        self._ask_human_question_row = build_answered_question_widget(
            ev.summary, event_no=ev.no, extra_classes="ask-human-question hidden",
        )
        self._trace_mount(self._ask_human_question_row, scroll=False)
```

(`_unmount_ask_human_banner` уже делает `self._ask_human_question_row.remove_class("hidden")` — менять не нужно: `.answered-question` без `.hidden` показывает рамку.)

- [ ] **Step 9: Прогнать юнит-тесты ask_human/preview-логики**

Run: `uv run pytest tests/tui/test_answered_question.py -q`
Expected: PASS. (Если есть тесты, ассертящие старый `ask_human:`-префикс — найти `git grep "ask_human:" tests/` и обновить ожидания на `? `.)

- [ ] **Step 10: Перегенерировать и проверить снапшот диалога**

Run: `uv run pytest tests/tui/test_snapshot_main_full_dialog.py --snapshot-update`
Открыть `tests/tui/__snapshots__/test_snapshot_main_full_dialog/test_main_full_dialog_snapshot.svg`.
Expected: отвеченный вопрос — в серой скруглённой рамке, маркер `?` янтарный, без `ask_human:`.

- [ ] **Step 11: Прогон без апдейта**

Run: `uv run pytest tests/tui/test_answered_question.py tests/tui/test_snapshot_main_full_dialog.py -q`
Expected: PASS.

- [ ] **Step 12: Commit**

```bash
git add src/tui/palette.py src/tui/styles.tcss src/tui/services/trace_render.py src/tui/screens/main.py tests/tui/test_answered_question.py tests/tui/__snapshots__/test_snapshot_main_full_dialog
git commit -m "feat(tui): отвеченный вопрос в серой round-рамке с янтарным маркером, без ask_human:"
```

---

### Task 5: Стартовая карточка A2 — рамка + тэглайн на нижней линии

**Files:**
- Modify: `src/tui/screens/main.py` (`_show_brand`)
- Modify: `src/tui/styles.tcss` (`#brand-block`, удалить `.brand-tagline-row`/`.brand-rule`/`.brand-tagline`)
- Test: `tests/tui/test_snapshot_empty_state.py` (перегенерация)

**Interfaces:**
- Consumes: `tui.branding.logo_text()`, `tui.branding.tagline_display()`.
- Produces: `#brand-block` — `Vertical` со скруглённой teal-рамкой и `border_subtitle` = тэглайн. (В Task 6 в неё добавится правая MCP-колонка.)

- [ ] **Step 1: Перестроить `_show_brand` на карточку с subtitle**

В `src/tui/screens/main.py`, заменить тело `_show_brand` (логика монтирования; tagline_row и две `brand-rule` убираем — тэглайн уходит в `border_subtitle`):

```python
    def _show_brand(self) -> None:
        if self._brand is not None and self._brand.parent is not None:
            return
        trace = self.query_one("#trace", VerticalScroll)
        brand = Vertical(
            Static(branding.logo_text(), classes="brand-logo"),
            id="brand-block",
        )
        brand.border_subtitle = branding.tagline_display()
        trace.mount(brand)
        self._brand = brand
```

- [ ] **Step 2: CSS карточки + удалить осиротевшие классы тэглайна**

В `src/tui/styles.tcss`, заменить правило `#brand-block`:

```css
#brand-block {
    width: 1fr;
    height: auto;
    margin: 1 1 0 1;
    padding: 0 1;
    border: round $accent;
    border-subtitle-color: $dim;
}
```

Удалить ставшие неиспользуемыми правила `.brand-tagline-row`, `.brand-rule`, `.brand-tagline` (логотип-класс `.brand-logo` оставить).

- [ ] **Step 3: Перегенерировать и проверить empty-state**

Run: `uv run pytest tests/tui/test_snapshot_empty_state.py --snapshot-update`
Открыть `tests/tui/__snapshots__/test_snapshot_empty_state/test_empty_state_snapshot.svg`.
Expected: логотип в скруглённой teal-рамке; тэглайн «U N I V E R S A L   A G E N T» — на нижней линии рамки (dim); между линией хедера и верхом рамки — пустая строка-зазор.

- [ ] **Step 4: Прогон branding + empty-state**

Run: `uv run pytest tests/tui/test_branding.py tests/tui/test_snapshot_empty_state.py -q`
Expected: PASS (branding-тесты не затронуты — `tagline_display`/`logo_text` без изменений).

- [ ] **Step 5: Commit**

```bash
git add src/tui/screens/main.py src/tui/styles.tcss tests/tui/__snapshots__/test_snapshot_empty_state
git commit -m "feat(tui): стартовый бренд в скруглённой teal-карточке, тэглайн на нижней линии"
```

---

### Task 6: A2 — MCP справа в карточке + адаптив по ширине

**Files:**
- Modify: `src/tui/screens/main.py` (`_show_brand`, `_update_mcp_indicator`, `_settle_mcp_row`, `_TRACE_HEADER_IDS`)
- Modify: `src/tui/styles.tcss` (раскладка карточки: логотип слева / MCP справа; адаптив)
- Test: `tests/tui/test_mcp_boot_snapshot.py`, `tests/tui/test_mcp_snapshot.py` (перегенерация)

**Interfaces:**
- Consumes: `tui.services.mcp_progress.format_mcp_progress`, `format_mcp_settle`; `#brand-block` из Task 5.
- Produces: MCP-индикатор живёт внутри `#brand-block` как правая колонка `#brand-mcp` (Static), а не отдельным `#mcp-status-row` ребёнком `#trace`.

- [ ] **Step 1: Карточка = Horizontal(логотип, MCP-колонка)**

В `src/tui/screens/main.py`, `_show_brand` — обернуть логотип и MCP-колонку в `Horizontal`. MCP-Static создаём пустым (заполнит `_update_mcp_indicator`):

```python
    def _show_brand(self) -> None:
        if self._brand is not None and self._brand.parent is not None:
            return
        trace = self.query_one("#trace", VerticalScroll)
        self._mcp_status_row = Static("", id="brand-mcp", classes="line-meta", markup=False)
        brand = Vertical(
            Horizontal(
                Static(branding.logo_text(), classes="brand-logo"),
                self._mcp_status_row,
                id="brand-row",
            ),
            id="brand-block",
        )
        brand.border_subtitle = branding.tagline_display()
        trace.mount(brand)
        self._brand = brand
```

- [ ] **Step 2: `_update_mcp_indicator` пишет в существующую колонку, не монтирует свою строку**

В `src/tui/screens/main.py`, `_update_mcp_indicator` — убрать ветку `if self._mcp_status_row is None: ... _trace_mount(...)`; колонка уже создана в `_show_brand`. Тело свести к обновлению текста:

```python
    def _update_mcp_indicator(self) -> None:
        if self._mcp_status_done:
            return
        progress = getattr(self.app, "_mcp_progress", None)
        if progress is None:
            return
        servers, _active = progress.snapshot()
        if not servers:
            return
        from tui.services.mcp_progress import format_mcp_progress, format_mcp_settle
        if self._mcp_status_row is None:
            return  # карточка ещё не смонтирована
        self._mcp_status_row.update(format_mcp_progress(servers))
        if progress.all_reported():
            self._mcp_status_done = True
            self.app._mcp_progress = None
            row = self._mcp_status_row
            self.set_timer(
                1.0, lambda: self._settle_mcp_row(row, format_mcp_settle(servers))
            )
```

(`_settle_mcp_row` не меняется — он делает `row.update(final)`.)

- [ ] **Step 3: Обновить инвариант шапки трейса**

В `src/tui/screens/main.py` обновить `_TRACE_HEADER_IDS` — MCP теперь внутри `#brand-block`, отдельного `#mcp-status-row` нет:

```python
    _TRACE_HEADER_IDS = frozenset({"brand-block"})
```

- [ ] **Step 4: CSS раскладки карточки + адаптив**

В `src/tui/styles.tcss` добавить (после `#brand-block`):

```css
#brand-row { width: 1fr; height: auto; }
.brand-logo { color: $brand; text-style: bold; width: auto; height: auto; }
#brand-mcp  { width: 1fr; color: $deep-dim; padding-left: 2; height: auto; content-align: left top; }
```

(Старое правило `.brand-logo { ... width: 100% ... }` заменить на `width: auto`, чтобы логотип не растягивался и MCP встал справа.)

- [ ] **Step 5: Перегенерировать MCP-снапшоты и проверить**

Run: `uv run pytest tests/tui/test_mcp_boot_snapshot.py tests/tui/test_mcp_snapshot.py --snapshot-update`
Открыть обновлённые `.svg` в `tests/tui/__snapshots__/`.
Expected: при boot — логотип слева, список MCP-серверов справа в той же teal-карточке; по готовности — «✓ MCP: все подключены / готов к работе» справа.

- [ ] **Step 6: Адаптив — fallback на узкой ширине**

Проверить boot-снапшот на узком терминале: во `_show_brand` MCP-колонка `width: 1fr` уже переносит контент под логотип средствами Textual при нехватке ширины (flex). Если визуально логотип ломается на терминале < (logo_width + ~20) — добавить в `_show_brand` ветку: при `self.app.size.width < 84` создавать карточку с вертикальной раскладкой (логотип, затем MCP) вместо `Horizontal`. Зафиксировать порог константой:

```python
    _BRAND_WIDE_MIN = 84  # ниже — MCP под логотип (адаптив A2)
```

И в `_show_brand` выбирать `Horizontal` (≥ порога) либо `Vertical` (ниже). Критерий приёмки — логотип не рвётся ни на 80, ни на 120 колонках.

- [ ] **Step 7: Прогон без апдейта**

Run: `uv run pytest tests/tui/test_mcp_boot_snapshot.py tests/tui/test_mcp_snapshot.py tests/tui/test_snapshot_empty_state.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/tui/screens/main.py src/tui/styles.tcss tests/tui/__snapshots__/test_mcp_boot_snapshot tests/tui/__snapshots__/test_mcp_snapshot
git commit -m "feat(tui): MCP-блок справа в стартовой карточке A2 + адаптив по ширине"
```

---

### Task 7: Финальная регрессия и сверка

**Files:** — (без правок кода; проверка)

- [ ] **Step 1: Полный прогон TUI-тестов**

Run: `uv run pytest tests/tui -q`
Expected: PASS (baseline-падения вне TUI не учитываем; снапшоты зелёные).

- [ ] **Step 2: Сверка токенов**

Run: `git grep -n "frame-archive" src/tui/styles.tcss; git grep -n "FRAME_ARCHIVE" src/tui/palette.py`
Expected: токен присутствует в обоих файлах с одинаковым значением `#4b525d`.

- [ ] **Step 3: Сверка «не тронули запрещённое»**

Run: `git diff --stat master -- src/tui`
Expected: в диффе нет правок `TextArea.ask-human-mode`, геометрии `#header`, `markdown_render.py`.

- [ ] **Step 4 (ручная, после сборки):** Запустить `gc-ua` вживую: стартовый экран A2 (узкий/широкий терминал), баннер ask_human (янтарная round-рамка), guard write/command (`⚠`, teal round-рамка, ширина глифа ок), отвеченный вопрос (серая рамка), палитра/конфиг скруглены.

---

## Отложенные варианты (по запросу пользователя, после живого сравнения — НЕ в этом плане)

- **Стартовый экран B** («только линии», без боков): структура `_show_brand`/CSS факторизованы так, что B = альтернативный CSS-режим карточки. Реализовать по запросу для живого сравнения.
- **Янтарная рамка отвеченного вопроса:** сменить `border` в `.answered-question` на `$run-color` — единственная точка правки. Реализовать по запросу.

---

## Self-Review (выполнено при написании)

- **Покрытие спеки:** §3.1→Task 5+6, §3.2→Task 1, §3.3→Task 2, §3.4→Task 4, §3.5→Task 3, §3.6→Task 3, §4 токены→Task 4 (+Global), §6 тесты→каждая задача, §9 вне-объёма→Global Constraints + Task 7 Step 3. Отложенные варианты (§3.1 B, §3.4 янтарь) — отдельный раздел.
- **Плейсхолдеров нет:** весь код приведён; глиф/порог/токен заданы конкретными значениями.
- **Согласованность имён:** `build_guard_label`/`GUARD_GLYPH` (Task 2), `build_answered_question_widget` (Task 4, исп. в main+trace_render), `FRAME_ARCHIVE`/`$frame-archive` (Task 4), `#brand-block`/`#brand-row`/`#brand-mcp`/`_BRAND_WIDE_MIN`/`_TRACE_HEADER_IDS` (Task 5-6) — совпадают между задачами.
