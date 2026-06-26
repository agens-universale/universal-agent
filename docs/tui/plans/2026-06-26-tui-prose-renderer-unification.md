# Prose-first унификация рендера ответа — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Унифицировать прозовый рендер ответа между стримом и финалом, чтобы для прозы на финале не менялись ни пустые строки, ни отступ (вариант A — паритет с первого токена).

**Architecture:** Один прозовый рендерер контента (`render_prose`) + один двух-колоночный хост (`TraceRow`, `● `+hanging indent) в обоих контекстах. Финал переходит с по-строчного `render_block` на по-прогонную сегментацию (`segment_answer`, пустые строки сохранены), прозу рисует `render_prose`. Код/таблицы — без изменений (prose-first граница). Reasoning-«глазок» — без изменений.

**Tech Stack:** Python 3.10+, Textual/rich, pytest (TUI → `uv run pytest`).

**Spec:** [docs/tui/specs/2026-06-26-tui-prose-renderer-unification-design.md](../specs/2026-06-26-tui-prose-renderer-unification-design.md)

## Global Constraints

- Вариант A: стрим-проза сразу `● ` + висячий отступ + пустые строки, идентично финалу.
- Прозовый рендер контента — единый `render_prose(text)->rich.Text` (переименование `render_stream_answer`, тело без изменений).
- Хост прозы — существующий `TraceRow` (двух-колоночный). Код/таблицы — отдельные виджеты, без изменений (на финале — бокс/сетка).
- Финал — общий рендер `build_block_widgets` для всех режимов `stream_display` (+ превью + старые треды); улучшение прозы получают все, включая `minimal`.
- НЕ трогать: reasoning-«глазок» (`StreamBlock`), агент/finish-gate/суммаризацию, CLI/server-рендер.
- Все clear-пути эфемерного стрим-виджета идут через `_clear_stream_block` — единственный чокпойнт.
- Абсолютные импорты от `src/`. TUI-тесты — `uv run pytest`. Линтера нет.
- Коммиты — лаконичные, по-русски/смешанно; без AI-атрибуции, без `Co-Authored-By`; `git add` только перечисленные пути; не коммитить `.claude/`/`CLAUDE.md`. Ветка `feat/agent-instructions-and-gc-guard` (не пушить).

---

### Task 1: Переименование `render_stream_answer` → `render_prose`

**Files:**
- Modify: `src/tui/services/markdown_render.py:163` (имя функции + докстринг)
- Modify: `src/tui/screens/main.py:1515,1524` (импорт + вызов)
- Rename+Modify: `tests/tui/test_render_stream_answer.py` → `tests/tui/test_render_prose.py`

**Interfaces:**
- Produces: `render_prose(text: str) -> rich.text.Text` (бывший `render_stream_answer`, поведение идентично).

- [ ] **Step 1: Переименовать тест-файл и его обращения (RED)**

`git mv tests/tui/test_render_stream_answer.py tests/tui/test_render_prose.py`, затем заменить в нём ВСЕ `render_stream_answer` → `render_prose` (импорт строки 1 + 7 вызовов). Дополнительно усилить покрытие стилей (перенос из удаляемых render_block_to_text-тестов, Task 3) — добавить два теста:

```python
def test_heading_is_accent_bold():
    t = render_prose("# Заголовок")
    assert t.plain == "Заголовок"
    assert any("54b3a6" in str(s.style).lower() and "bold" in str(s.style).lower()
               for s in t.spans)


def test_para_bold_and_code_styled_spans():
    t = render_prose("есть **bold** и `code`")
    assert "**" not in t.plain and "`" not in t.plain
    assert any("54b3a6" in str(s.style).lower() for s in t.spans)   # accent (bold)
    assert any("afbdff" in str(s.style).lower() for s in t.spans)   # code (slate)
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_render_prose.py -v`
Expected: FAIL — `render_prose` ещё не существует (есть `render_stream_answer`).

- [ ] **Step 3: Переименовать функцию**

В `src/tui/services/markdown_render.py:163` — `def render_stream_answer(text: str) -> Text:` → `def render_prose(text: str) -> Text:`; в докстринге «V1 live-answer рендер» → «Прозовый рендер (стрим и финал)». Тело без изменений.

В `src/tui/screens/main.py`: строка 1515 `from tui.services.markdown_render import render_stream_answer` → `import render_prose`; строка 1524 `renderable = render_stream_answer(clean)` → `renderable = render_prose(clean)`.

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/tui/test_render_prose.py -v`
Expected: PASS (9).

- [ ] **Step 5: Регресс стрима**

Run: `uv run pytest tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py -q`
Expected: PASS (стрим всё ещё работает — имя сменилось, поведение нет).

- [ ] **Step 6: Commit**

```bash
git add src/tui/services/markdown_render.py src/tui/screens/main.py tests/tui/test_render_prose.py
git commit -m "refactor(tui): render_stream_answer -> render_prose (станет общим для стрима и финала)"
```

---

### Task 2: `TraceRow.set_content`

**Files:**
- Modify: `src/tui/widgets/trace_row.py` (добавить метод рядом с `set_prefix`, ~:85)
- Test: `tests/tui/test_trace_row_set_content.py` (new)

**Interfaces:**
- Produces: `TraceRow.set_content(content)` — обновляет `.trace-content` Static после монтирования (зеркало `set_prefix`).

- [ ] **Step 1: Failing-тест**

Создать `tests/tui/test_trace_row_set_content.py`:

```python
import pytest
from textual.app import App
from textual.widgets import Static
from tui.widgets.trace_row import TraceRow


@pytest.mark.asyncio
async def test_set_content_updates_content_static():
    class _App(App):
        def compose(self):
            yield TraceRow(prefix="● ", content="старый", kind_class="line-agent")

    app = _App()
    async with app.run_test():
        row = app.query_one(TraceRow)
        row.set_content("новый текст")
        content = row.query_one(".trace-content", Static)
        assert "новый текст" in str(content.render())
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_trace_row_set_content.py -v`
Expected: FAIL — `set_content` не существует (AttributeError).

- [ ] **Step 3: Реализовать**

В `src/tui/widgets/trace_row.py` после `set_prefix` (~:91) добавить:

```python
    def set_content(self, content) -> None:
        """Обновить content-колонку после монтирования (пер-тик апдейт стрим-прозы)."""
        self._content = content
        try:
            self.query_one(".trace-content", Static).update(content)
        except Exception:
            pass
```

- [ ] **Step 4: Тест зелёный**

Run: `uv run pytest tests/tui/test_trace_row_set_content.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/tui/widgets/trace_row.py tests/tui/test_trace_row_set_content.py
git commit -m "feat(tui): TraceRow.set_content — обновление контента после монтирования"
```

---

### Task 3: Финал — `segment_answer` (прозовые прогоны) + `build_block_widgets` ⚠️

**Files:**
- Modify: `src/tui/services/markdown_render.py` (добавить `segment_answer`; удалить `render_block`, `render_block_to_text`)
- Modify: `src/tui/services/trace_render.py:64-107` (`build_block_widgets` → segment_answer + render_prose)
- Modify: `tests/tui/test_markdown_render.py` (render_block/render_block_to_text тесты → segment_answer; table-тесты сохранить)

**Interfaces:**
- Consumes: `render_prose` (Task 1); существующие `_is_table_separator`/`_split_table_row`/`_parse_table_aligns`/`build_markdown_table`, `Block`.
- Produces: `segment_answer(text: str) -> list[Block]` — `kind ∈ {'prose','code-block','table'}`; `prose` → `content` = verbatim текст прогона (пустые строки внутри сохранены, по краям срезаны). `build_block_widgets` рисует prose одним `TraceRow(render_prose(content))`.

- [ ] **Step 1: Заменить тесты markdown_render (RED)**

Полностью заменить `tests/tui/test_markdown_render.py` на (segment_answer + table; стили прозы теперь в `test_render_prose.py`):

```python
"""segment_answer: сегментация ответа на прозовые прогоны + code/table-блоки."""
from tui.services.markdown_render import Block, segment_answer, build_markdown_table


def test_pure_prose_is_one_run():
    assert segment_answer("просто текст") == [Block("prose", "просто текст")]


def test_blank_lines_preserved_inside_run():
    assert segment_answer("абзац1\n\nабзац2") == [Block("prose", "абзац1\n\nабзац2")]


def test_heading_and_bullet_stay_in_prose_run():
    # синтаксис НЕ срезаем — render_prose отрисует # и - построчно
    assert segment_answer("# Title\n- item") == [Block("prose", "# Title\n- item")]


def test_edge_blank_lines_trimmed():
    assert segment_answer("\n\nтекст\n\n") == [Block("prose", "текст")]


def test_fenced_with_lang_is_code_block():
    assert segment_answer("```python\nx = 1\ny = 2\n```") == [
        Block("code-block", "x = 1\ny = 2", "python")]


def test_fenced_no_lang():
    assert segment_answer("```\nx = 1\n```") == [Block("code-block", "x = 1", None)]


def test_mixed_prose_and_code_splits_runs():
    text = "Вот пример:\n```js\nconst x = 1;\n```\nКонец."
    assert segment_answer(text) == [
        Block("prose", "Вот пример:"),
        Block("code-block", "const x = 1;", "js"),
        Block("prose", "Конец."),
    ]


def test_table_basic():
    segs = segment_answer("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |")
    assert len(segs) == 1 and segs[0].kind == "table"
    assert segs[0].headers == ["A", "B"]
    assert segs[0].rows == [["1", "2"], ["3", "4"]]


def test_table_alignment_markers():
    segs = segment_answer("| L | C | R |\n|:--|:-:|--:|\n| a | b | c |")
    assert segs[0].aligns == ["left", "center", "right"]


def test_pipe_line_without_separator_is_prose():
    assert segment_answer("a | b | c\nплейн текст") == [Block("prose", "a | b | c\nплейн текст")]


def test_table_ragged_row_padded():
    segs = segment_answer("| A | B | C |\n|---|---|---|\n| 1 | 2 |")
    assert segs[0].rows == [["1", "2", ""]]


def test_build_markdown_table_smoke():
    t = build_markdown_table(["A", "B"], [["1", "2"]], ["left", "left"])
    assert t.columns[0].header == "A"
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_markdown_render.py -v`
Expected: FAIL — `segment_answer` не существует.

- [ ] **Step 3: Добавить `segment_answer`, удалить `render_block`/`render_block_to_text`**

В `src/tui/services/markdown_render.py` удалить функции `render_block` (строки 70-122) и `render_block_to_text` (строки 191-…конец функции). Оставить `Block`, `_is_table_separator`, `_split_table_row`, `_parse_table_aligns`, `build_markdown_table`, `_apply_inline_styles`, `render_prose`, `_STREAM_HEADING_RE`, `_STREAM_BULLET_RE`. Добавить:

```python
def segment_answer(text: str) -> list[Block]:
    """Сегментирует ответ на прозовые прогоны (verbatim, пустые строки внутри
    сохранены, по краям срезаны) и code/table-блоки. kind ∈ {'prose','code-block','table'}."""
    segs: list[Block] = []
    lines = text.split("\n")
    i = 0
    prose: list[str] = []

    def _flush() -> None:
        run = prose[:]
        while run and not run[0].strip():
            run.pop(0)
        while run and not run[-1].strip():
            run.pop()
        if run:
            segs.append(Block("prose", "\n".join(run)))
        prose.clear()

    while i < len(lines):
        ln = lines[i]
        if ln.startswith("```"):
            _flush()
            lang_tag = ln[3:].strip() or None
            j = i + 1
            chunk: list[str] = []
            while j < len(lines) and not lines[j].startswith("```"):
                chunk.append(lines[j])
                j += 1
            segs.append(Block("code-block", "\n".join(chunk), lang_tag))
            i = j + 1
            continue
        if "|" in ln and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            _flush()
            headers = _split_table_row(ln)
            ncols = len(headers)
            aligns = _parse_table_aligns(lines[i + 1], ncols)
            rows: list[list[str]] = []
            j = i + 2
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                rows.append((_split_table_row(lines[j]) + [""] * ncols)[:ncols])
                j += 1
            segs.append(Block("table", headers=headers, rows=rows, aligns=aligns))
            i = j
            continue
        prose.append(ln)
        i += 1
    _flush()
    return segs
```

- [ ] **Step 4: Переключить `build_block_widgets` на segment_answer + render_prose**

В `src/tui/services/trace_render.py` заменить тело `build_block_widgets` (строки 70-107) на:

```python
    from tui.services.markdown_render import (
        build_markdown_table, render_prose, segment_answer,
    )
    from tui.widgets.trace_row import TraceRow

    widgets: list = []
    focus_set = False
    first_prose_seen = False
    for seg in segment_answer(text):
        if seg.kind == "code-block":
            widgets.append(render_code_block(
                seg.content, seg.lang, event_no=event_no,
                focusable=not focus_set, indent=prose_bullet,
            ))
            focus_set = True
            continue
        if seg.kind == "table":
            table_classes = "table-block table-indent" if prose_bullet else "table-block"
            widgets.append(AgentReplyLine(
                build_markdown_table(seg.headers or [], seg.rows or [], seg.aligns or []),
                event_no=event_no, focusable=not focus_set, classes=table_classes,
            ))
            focus_set = True
            continue
        kind_class = kind_class_base if not first_prose_seen else f"{kind_class_base} line-agent-cont"
        prefix = (bullet if not first_prose_seen else "  ") if prose_bullet else ""
        first_prose_seen = True
        widgets.append(TraceRow(
            prefix=prefix, content=render_prose(seg.content),
            kind_class=kind_class, event_no=event_no, focusable=not focus_set,
        ))
        focus_set = True
    return widgets
```

- [ ] **Step 5: Тесты зелёные + регресс трейс-рендера**

Run: `uv run pytest tests/tui/test_markdown_render.py tests/tui/test_trace_render.py -v`
Expected: PASS. (`test_trace_render` проверяет prose→TraceRow без буллета, table/code-indent — поведение сохранено.) Если в `test_trace_render` есть проверка числа prose-виджетов на многострочном вводе — обновить под «один TraceRow на прогон».

- [ ] **Step 6: Commit**

```bash
git add src/tui/services/markdown_render.py src/tui/services/trace_render.py tests/tui/test_markdown_render.py
git commit -m "feat(tui): финал рисует прозу по-прогонно (segment_answer + render_prose) — пустые строки сохранены"
```

---

### Task 4: Стрим — ответ как `TraceRow` (`● `+отступ), эфемерный слот ⚠️

**Files:**
- Modify: `src/tui/screens/main.py` (новый слот `_stream_answer_row`:~122; `_clear_stream_block`:694; стрим-ветка `_poll_active`:1511-1542)
- Modify: `tests/tui/test_stream_block_render.py` (answer-режимные тесты → новый слот)

**Interfaces:**
- Consumes: `render_prose` (Task 1), `TraceRow.set_content` (Task 2), `select_stream_render` (существует).
- Produces: эфемерный ответ стрима = `TraceRow(prefix="● ", content=render_prose(clean), kind_class="line-agent", focusable=False)` в слоте `self._stream_answer_row`; reasoning остаётся `StreamBlock` в `self._stream_block`; `_clear_stream_block` снимает ОБА.

- [ ] **Step 1: Мигрировать answer-режимные тесты (RED)**

В `tests/tui/test_stream_block_render.py` — у тестов, проверяющих ОТВЕТ (не reasoning), заменить запрос `StreamBlock` на слот `_stream_answer_row`:

`test_stream_block_mounts_then_clears_on_canonical_row` (~L82-95): заменить
```python
        blocks = pilot.app.query(StreamBlock)
        assert len(blocks) == 1
        assert str(blocks[0].render()) == "Это проект."
```
на
```python
        assert scr._stream_answer_row is not None
        from textual.widgets import Static
        content = scr._stream_answer_row.query_one(".trace-content", Static)
        assert "Это проект." in str(content.render())
```
и ниже (после `_render_record` канонической строки) `assert len(pilot.app.query(StreamBlock)) == 0` → `assert scr._stream_answer_row is None`.

`test_reset_render_state_clears_stream_block_and_sink` (~L121-128): `assert len(pilot.app.query(StreamBlock)) == 1` → `assert scr._stream_answer_row is not None`; после `_reset_render_state` — `assert len(pilot.app.query(StreamBlock)) == 0` → дополнить `and scr._stream_answer_row is None`.

`test_stream_block_cleared_when_run_not_running` (~L155-161): аналогично — до `runner.stop()`: `assert scr._stream_answer_row is not None`; после: `assert scr._stream_answer_row is None`.

`test_stream_answer_preview_false_hides_answer_block` (~L327): `assert not pilot.app.query(StreamBlock)` → `assert scr._stream_answer_row is None`.

`test_show_reasoning_line_false_hides_reasoning_block` (~L399): режим `answer`, think-only → ни ответа, ни reasoning: оставить `assert not pilot.app.query(StreamBlock)` и добавить `assert scr._stream_answer_row is None`.

(reasoning-режимные тесты — `test_reasoning_line_below_committed_rows`, `test_reasoning_block_cleared_when_tool_row_renders`, `test_stream_answer_preview_false_shows_reasoning_line`, `test_no_reasoning_block_in_validating_phase` — НЕ трогать: reasoning остаётся `StreamBlock`.)

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: FAIL — `_stream_answer_row` не существует / ответ всё ещё в `StreamBlock`.

- [ ] **Step 3: Слот + `_clear_stream_block` снимает оба**

В `src/tui/screens/main.py:122` рядом с `self._stream_block = None` добавить:
```python
        self._stream_answer_row = None  # type: Optional["TraceRow"]
```
В `_clear_stream_block` (694) — после снятия `_stream_block` добавить снятие ответного ряда (перед `if reset_sink:`):
```python
        if self._stream_answer_row is not None:
            self._stream_answer_row.remove()
            self._stream_answer_row = None
```

- [ ] **Step 4: Стрим-ветка `_poll_active`**

Заменить блок строк 1523-1542 (от `if kind == "answer":` до `self._clear_stream_block(reset_sink=False)`) на:

```python
                from tui.widgets.trace_row import TraceRow
                following = self._trace_is_at_bottom()
                if kind == "answer":
                    if self._stream_block is not None:          # switch reasoning→answer
                        self._stream_block.remove()
                        self._stream_block = None
                    if self._stream_answer_row is None:
                        self._stream_answer_row = TraceRow(
                            prefix="● ", content=render_prose(clean),
                            kind_class="line-agent", focusable=False)
                        self._trace_mount(self._stream_answer_row, scroll=True)
                    else:
                        self._stream_answer_row.set_content(render_prose(clean))
                    if following:
                        self.query_one("#trace", VerticalScroll).scroll_end(animate=False)
                elif kind == "reasoning":
                    if self._stream_answer_row is not None:      # switch answer→reasoning
                        self._stream_answer_row.remove()
                        self._stream_answer_row = None
                    if self._stream_block is None:
                        self._stream_block = StreamBlock()
                        self._trace_mount(self._stream_block, scroll=True)
                    self._stream_block.set_text(reasoning, thinking=True)
                    if following:
                        self.query_one("#trace", VerticalScroll).scroll_end(animate=False)
                else:
                    self._clear_stream_block(reset_sink=False)
```

(удаляется прежняя сборка `renderable`/`if renderable is not None` — `select_stream_render` уже вернул `kind`; импорт `render_prose`/`StreamBlock` остаётся в начале ветки.)

- [ ] **Step 5: Тесты зелёные + регресс стрима/автоскролла**

Run: `uv run pytest tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py -v`
Expected: PASS (autoscroll шпионит `scroll_end`/`_trace_is_at_bottom` — тип виджета не важен).

- [ ] **Step 6: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_stream_block_render.py
git commit -m "feat(tui): стрим-ответ — TraceRow (● + висячий отступ) через render_prose, паритет с финалом"
```

---

### Task 5: Парити-тест + превью-паритет + финальная сверка

**Files:**
- Test: `tests/tui/test_prose_parity.py` (new)
- Test: `tests/tui/test_preview_prose.py` (new)

**Interfaces:**
- Consumes: `render_prose`, `segment_answer`, `build_block_widgets`, read-only превью (`render_thread_readonly`).

- [ ] **Step 1: Парити + превью тесты**

Создать `tests/tui/test_prose_parity.py`:

```python
"""Чистая проза: контент стрим-ряда и финал-ряда идентичны (оба render_prose)."""
from textual.widgets import Static

from tui.services.markdown_render import render_prose, segment_answer
from tui.services.trace_render import build_agent_widgets
from tui.widgets.trace_row import TraceRow


def test_pure_prose_stream_equals_final_content():
    text = "Первый абзац.\n\nВторой абзац после пустой строки."
    # финал: один прозовый прогон → один TraceRow с render_prose(text)
    segs = segment_answer(text)
    assert [s.kind for s in segs] == ["prose"]
    final_text = render_prose(segs[0].content)
    # стрим: тот же render_prose на том же тексте
    stream_text = render_prose(text)
    assert final_text.plain == stream_text.plain
    # пустая строка сохранена в обоих
    assert "\n\n" in final_text.plain


def test_final_multiparagraph_is_single_trace_row():
    ws = build_agent_widgets("абзац1\n\nабзац2", event_no=1)
    rows = [w for w in ws if isinstance(w, TraceRow)]
    assert len(rows) == 1                       # один прогон → один ряд (не по-строчно)
    assert rows[0]._content.plain == "абзац1\n\nабзац2"   # пустая строка сохранена
```

Создать `tests/tui/test_preview_prose.py`:

```python
"""Read-only превью рендерит прозу так же, как трейс (через build_block_widgets)."""
import pytest


@pytest.mark.asyncio
async def test_preview_multiparagraph_prose_single_row(tmp_path):
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll
    from tui.services.trace_render import render_thread_readonly
    from tui.widgets.trace_row import TraceRow

    rec = {"payload": {"id": "a1", "role": "assistant",
                       "content": "абзац1\n\nабзац2"}, "ts": ""}

    class _App(App):
        def compose(self):
            yield VerticalScroll(id="m")

    app = _App()
    async with app.run_test() as pilot:
        target = app.query_one("#m", VerticalScroll)
        render_thread_readonly(target, [rec])
        await pilot.pause()
        rows = [w for w in target.query(TraceRow)]
        prose_rows = [r for r in rows if "абзац" in str(r._content.plain)]
        assert len(prose_rows) == 1            # один прогон, пустая строка внутри
        assert "\n\n" in str(prose_rows[0]._content.plain)
```

- [ ] **Step 2: Запустить**

Run: `uv run pytest tests/tui/test_prose_parity.py tests/tui/test_preview_prose.py -v`
Expected: PASS (после Task 1-4). Если `_content` для TraceRow — `rich.Text`, у него есть `.plain`; парити-тест это и проверяет.

- [ ] **Step 3: Финальный sweep затронутого**

Run: `uv run pytest tests/tui/test_render_prose.py tests/tui/test_markdown_render.py tests/tui/test_trace_render.py tests/tui/test_trace_row_set_content.py tests/tui/test_stream_block_render.py tests/tui/test_stream_autoscroll.py tests/tui/test_prose_parity.py tests/tui/test_preview_prose.py -q`
Expected: PASS.

Run (чистота): `grep -rn "render_stream_answer\|render_block_to_text\|\brender_block\b" src/ tests/`
Expected: пусто (всё переехало на `render_prose`/`segment_answer`).

- [ ] **Step 4: Commit**

```bash
git add tests/tui/test_prose_parity.py tests/tui/test_preview_prose.py
git commit -m "test(tui): парити прозы стрим↔финал + превью-паритет (один прогон, пустые строки)"
```

- [ ] **Step 5 (опц.): доки**

```bash
git add docs/tui/specs/2026-06-26-tui-prose-renderer-unification-design.md docs/tui/plans/2026-06-26-tui-prose-renderer-unification.md
git commit -m "docs(tui): дизайн+план — prose-first унификация рендера ответа"
```

---

## Self-Review

**Spec coverage:**
- §render_prose (общий контент) → Task 1. ✓
- §TraceRow host + set_content → Task 2. ✓
- §финал по-прогонно (segment_answer + build_block_widgets, пустые строки) → Task 3. ✓
- §стрим-ответ как TraceRow (`● `+отступ), единый чокпойнт clear → Task 4. ✓
- §поведение по режимам (minimal: стрим как раньше; финал улучшается всем) → следствие Task 3 (общий рендер), Task 4 не монтирует ответ в minimal (select→none). ✓
- §регресс-поверхности: превью → Task 5 (test_preview_prose) + Task 3 (общий build_block_widgets); ask_human (prose_bullet=False) → Task 3 (`prefix=""`), покрыт test_trace_render; clear-пути → Task 4 (_clear_stream_block оба слота); focus/Tab → focusable=not focus_set сохранён (Task 3/4); CSS/вид → test_trace_render + парити; нейминг → Task 1 + grep чистоты (Task 5). ✓
- §критерий паритета → Task 5 (test_prose_parity). ✓
- §вне scope (код/таблицы, reasoning) — не трогаются. ✓

**Placeholder scan:** заглушек нет; весь код и команды приведены.

**Type consistency:** `render_prose(str)->Text` (Task 1) используется в Task 3/4/5; `segment_answer(str)->list[Block]` kind∈{prose,code-block,table} (Task 3) — потребитель build_block_widgets (Task 3) и тесты (Task 5); `TraceRow.set_content` (Task 2) — стрим (Task 4); слоты `_stream_block`/`_stream_answer_row` согласованы Task 4 ↔ `_clear_stream_block`.
