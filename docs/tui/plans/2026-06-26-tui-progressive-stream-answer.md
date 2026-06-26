# Прогрессивный показ ответа агента в TUI (V1) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать прогрессивный показ ответа агента (chat_completions) дефолтным и в нормальном виде (V1: inline-разметка, без серого скачка), заменив два булевых флага одним enum `tui.stream_display`.

**Architecture:** Правка в TUI-слое рендера + конфиге. Транспорт (`chat_completions_stream`), буфер (`StreamSink`), индикатор — НЕ трогаем. Шов «стрим→финал» уже реализован (`_render_event` → `_clear_stream_block()` при монтаже канонической строки, main.py:804) — нового шов-кода нет. V1: `render_stream_answer` (markdown_render) даёт INK-Text с inline-стилями (fenced/таблицы — plain, защёлкиваются на финале); `select_stream_render` (stream_sink) — чистое решение режима; `_poll_active` их связывает.

**Tech Stack:** Python 3.10+, Textual/rich, pydantic, pytest (TUI/конфиг → `uv run pytest`).

**Spec:** [docs/tui/specs/2026-06-26-tui-progressive-stream-answer-design.md](../specs/2026-06-26-tui-progressive-stream-answer-design.md)

## Global Constraints

- Флаг-модель: `TuiConfig.stream_display: Literal["minimal","answer","answer_reasoning"] = "answer"`; два старых поля (`show_reasoning_line`, `show_stream_answer`) **удалить** (clean break, без миграции/алиасов; `extra="forbid"` остаётся).
- Флэттенинг: один ключ `tui_stream_display` (config.py).
- **Обязательно** пронести в шипованные конфиги: `config.default.yml`, `demo_config.yml`, и таблицу фич `docs/CHANGES-vs-master.md`.
- Транспорт/`StreamSink`/`ThinkingIndicator`/шов (main.py:804) — НЕ трогаем. Поведение агента/finish-gate/суммаризации — НЕ трогаем. Только `chat_completions`.
- Цвет ответа — INK (`$ink` = `#ebe9e6`), не серый. V1: fenced-блоки/таблицы во время стрима — plain.
- Абсолютные импорты от `src/`. TUI/конфиг-тесты — `uv run pytest`. Линтера нет — вслепую не гонять.
- Коммиты — лаконичные, по-русски/смешанно; без AI-атрибуции, без `Co-Authored-By`. НЕ коммитить `.claude/`, `CLAUDE.md`. `git add` — только перечисленные пути.

---

### Task 1: Флаг-модель — enum `stream_display` + flatten + шипованные конфиги/доки

**Files:**
- Modify: `src/config_modules/tui.py` (убрать 2 поля, добавить `stream_display`)
- Modify: `src/config.py:196-197` (flatten 2 строки → 1)
- Modify: `config.default.yml:45-49`
- Modify: `demo_config.yml:56-60` (комментарий) и `:75-83` (блок)
- Modify: `docs/CHANGES-vs-master.md:62-63` (2 строки → 1)
- Modify (tests): `tests/test_tui_config.py` (заменить старые тесты на новые)

**Interfaces:**
- Produces: `TuiConfig.stream_display` (Literal, default `"answer"`); плоский ключ `tui_stream_display`. Старые `show_reasoning_line`/`show_stream_answer` и `tui_show_*` исчезают.

- [ ] **Step 1: Заменить тесты конфига (RED)**

Заменить ВСЁ содержимое `tests/test_tui_config.py` на:

```python
import pytest
from config_modules.tui import TuiConfig


def test_tui_default_stream_display_answer():
    assert TuiConfig().stream_display == "answer"


def test_tui_stream_display_accepts_three_values():
    for v in ("minimal", "answer", "answer_reasoning"):
        assert TuiConfig(stream_display=v).stream_display == v


def test_tui_stream_display_rejects_unknown():
    with pytest.raises(Exception):
        TuiConfig(stream_display="loud")


def test_tui_rejects_removed_flags():
    with pytest.raises(Exception):
        TuiConfig(show_stream_answer=True)
    with pytest.raises(Exception):
        TuiConfig(show_reasoning_line=False)


def test_flatten_exposes_tui_stream_display(tmp_path, monkeypatch):
    """Значение из config.yml доходит до плоского config_dict (overlay нейтрализованы)."""
    import config as cfgmod
    import mcp_runtime.paths as mpaths

    monkeypatch.setattr(cfgmod, "_project_local_config_path", lambda: None)
    monkeypatch.setattr(mpaths, "managed_config_path", lambda: tmp_path / "none.yml")

    conf = tmp_path / "config.yml"
    conf.write_text("tui:\n  stream_display: minimal\n", encoding="utf-8")
    cfg = cfgmod.load_config(str(conf))
    assert cfg["tui_stream_display"] == "minimal"
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/test_tui_config.py -v`
Expected: FAIL — `stream_display` нет; старые поля принимаются; `tui_stream_display` отсутствует.

- [ ] **Step 3: TuiConfig — заменить два поля на enum**

В `src/config_modules/tui.py` заменить строки 9-10 (`show_reasoning_line`/`show_stream_answer`) на:

```python
    # Что рендерить во время стрима (только chat_completions + backend.stream:true;
    # на счётчик токенов/индикатор не влияет — только контент):
    #   minimal          — индикатор + счётчик, без текста стрима;
    #   answer (default) — прогрессивный ответ, дорастающий до финала;
    #   answer_reasoning — + однострочный «глазок» рассуждения «⠇ …».
    stream_display: Literal["minimal", "answer", "answer_reasoning"] = "answer"
```

(`Literal` уже импортирован; `text_answer_mode` ниже — без изменений.)

- [ ] **Step 4: config.py — flatten одного ключа**

В `src/config.py` заменить строки 196-197:

```python
        "tui_show_reasoning_line": app_cfg.tui.show_reasoning_line,
        "tui_show_stream_answer": app_cfg.tui.show_stream_answer,
```

на:

```python
        "tui_stream_display": app_cfg.tui.stream_display,
```

(строка `"tui_text_answer_mode": ...` остаётся.)

- [ ] **Step 5: Шипованные конфиги + таблица фич**

`config.default.yml` — заменить строки 45-49 на:

```yaml
# TUI: что рендерить во время стриминга (действует только при backend.stream: true;
# на счётчик токенов и индикатор не влияет — только контент).
tui:
  # minimal — только индикатор + счётчик; answer (default) — прогрессивный ответ;
  # answer_reasoning — + строка-мысль «⠇ …».
  stream_display: answer
```

`demo_config.yml` — в комментарии строки 59-60 заменить упоминание двух флагов:
`Флаги tui.show_reasoning_line и tui.show_stream_answer (ниже) имеют эффект ТОЛЬКО`
→ `Флаг tui.stream_display (ниже) имеет эффект ТОЛЬКО`.
И заменить блок строк 78-83 (комментарии + два ключа) на:

```yaml
tui:
  # Что показывать во время стрима:
  #   minimal — только индикатор + счётчик токенов (без текста);
  #   answer (default) — прогрессивный ответ, дорастающий до финала;
  #   answer_reasoning — + живая строка-мысль «⠇ …».
  stream_display: answer
```

`docs/CHANGES-vs-master.md` — заменить строки 62-63 (две строки про старые флаги) на одну:

```
| Фича | Прогрессивный стрим ответа (V1) | ответ по мере генерации, как у харнесов | показать ответ заранее, чисто | `tui.stream_display` · **`answer`** (`minimal`/`answer_reasoning`) | TUI |
```

- [ ] **Step 6: Тесты зелёные**

Run: `uv run pytest tests/test_tui_config.py -v`
Expected: PASS (5).

- [ ] **Step 7: Регресс конфигов + чистота старых ключей**

Run: `uv run pytest tests/ -k config -q`
Expected: PASS (загрузка `config.default.yml`/`demo_config.yml` против схемы не падает).

Run: `grep -rn "show_stream_answer\|show_reasoning_line\|tui_show_stream_answer\|tui_show_reasoning_line" src/ config.default.yml demo_config.yml docs/CHANGES-vs-master.md`
Expected: пусто (старых ключей не осталось; main.py-ридеры — в Task 3).

- [ ] **Step 8: Commit**

```bash
git add src/config_modules/tui.py src/config.py config.default.yml demo_config.yml docs/CHANGES-vs-master.md tests/test_tui_config.py
git commit -m "feat(tui): флаг tui.stream_display (minimal|answer|answer_reasoning) вместо двух булевых"
```

---

### Task 2: Рендер-юниты — `render_stream_answer` + `select_stream_render` + StreamBlock/стиль

**Files:**
- Modify: `src/tui/services/markdown_render.py` (добавить `render_stream_answer` + 2 regex)
- Modify: `src/tui/services/stream_sink.py` (добавить `select_stream_render`)
- Modify: `src/tui/widgets/stream_block.py` (`set_text` принимает renderable)
- Modify: `src/tui/styles.tcss:205` (`$deep-dim` → `$ink`)
- Test: `tests/tui/test_render_stream_answer.py` (new), `tests/tui/test_select_stream_render.py` (new)

**Interfaces:**
- Produces:
  - `render_stream_answer(text: str) -> rich.text.Text` (markdown_render) — V1 inline-рендер.
  - `select_stream_render(mode: str, clean_answer: str, reasoning_line: str) -> tuple[str, bool]` (stream_sink) — `kind ∈ {"answer","reasoning","none"}`, и `thinking`.
- Consumes: `markdown_render._apply_inline_styles`, `_INK`, `_ACC` (в том же модуле).

- [ ] **Step 1: Тесты обоих юнитов (RED)**

Создать `tests/tui/test_render_stream_answer.py`:

```python
from tui.services.markdown_render import render_stream_answer


def test_plain_prose_kept():
    assert render_stream_answer("просто текст").plain == "просто текст"


def test_bold_markers_stripped():
    t = render_stream_answer("это **жирно**")
    assert "жирно" in t.plain and "**" not in t.plain


def test_inline_code_backticks_stripped():
    t = render_stream_answer("вот `код`")
    assert "код" in t.plain and "`" not in t.plain


def test_heading_hashes_stripped():
    assert render_stream_answer("# Заголовок").plain == "Заголовок"


def test_bullet_marker_normalized():
    assert render_stream_answer("- пункт").plain.startswith("• пункт")


def test_fenced_lines_stay_plain():
    t = render_stream_answer("```python\nx = 1\n```")
    assert "x = 1" in t.plain and "```python" in t.plain


def test_empty_text_empty():
    assert render_stream_answer("").plain == ""
```

Создать `tests/tui/test_select_stream_render.py`:

```python
from tui.services.stream_sink import select_stream_render


def test_minimal_is_none():
    assert select_stream_render("minimal", "ответ", "⠇ мысль") == ("none", False)


def test_answer_when_clean_present():
    assert select_stream_render("answer", "ответ", "⠇ мысль") == ("answer", False)


def test_answer_mode_empty_clean_is_none():
    assert select_stream_render("answer", "", "⠇ мысль") == ("none", False)


def test_answer_reasoning_prefers_answer():
    assert select_stream_render("answer_reasoning", "ответ", "⠇ м") == ("answer", False)


def test_answer_reasoning_shows_reasoning_without_answer():
    assert select_stream_render("answer_reasoning", "", "⠇ мысль") == ("reasoning", True)


def test_answer_reasoning_no_reasoning_is_none():
    assert select_stream_render("answer_reasoning", "", "") == ("none", False)
```

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_render_stream_answer.py tests/tui/test_select_stream_render.py -v`
Expected: FAIL — обеих функций нет.

- [ ] **Step 3: `render_stream_answer` в markdown_render**

В `src/tui/services/markdown_render.py` добавить (после `_apply_inline_styles`):

```python
_STREAM_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_STREAM_BULLET_RE = re.compile(r"^\s*[-*]\s+")


def render_stream_answer(text: str) -> Text:
    """V1 live-answer рендер: построчно inline-стили (**bold**, `code`) + line-стили
    heading/bullet; fenced-блоки и таблицы — plain (рамки/сетка только на финале).
    Возвращает rich.Text в нормальном цвете (INK)."""
    out = Text()
    in_fence = False
    for i, line in enumerate(text.split("\n")):
        if i:
            out.append("\n")
        if line.startswith("```"):
            in_fence = not in_fence
            out.append(line, style=_INK)
            continue
        if in_fence:
            out.append(line, style=_INK)
            continue
        m = _STREAM_HEADING_RE.match(line)
        if m:
            out.append(m.group(2).strip(), style=f"{_ACC} bold")
            continue
        if _STREAM_BULLET_RE.match(line):
            out.append("• ", style=_INK)
            _apply_inline_styles(out, _STREAM_BULLET_RE.sub("", line))
            continue
        _apply_inline_styles(out, line)
    return out
```

- [ ] **Step 4: `select_stream_render` в stream_sink**

В `src/tui/services/stream_sink.py` добавить (после `format_reasoning_line`):

```python
def select_stream_render(
    mode: str, clean_answer: str, reasoning_line: str
) -> tuple[str, bool]:
    """Что показать в StreamBlock по режиму tui_stream_display.
    Возвращает (kind, thinking): kind ∈ {"answer", "reasoning", "none"}."""
    if mode == "minimal":
        return ("none", False)
    if clean_answer.strip():
        return ("answer", False)
    if mode == "answer_reasoning" and reasoning_line:
        return ("reasoning", True)
    return ("none", False)
```

- [ ] **Step 5: StreamBlock принимает renderable + цвет**

В `src/tui/widgets/stream_block.py`: добавить импорт `from rich.console import RenderableType`
и сменить сигнатуру `set_text`:

```python
    def set_text(self, text: "RenderableType", *, thinking: bool = False) -> None:
```

(тело без изменений — `Static.update` принимает и `str`, и `rich.Text`.)

В `src/tui/styles.tcss` строка 205: `.stream-block { color: $deep-dim; ... }` → `color: $ink;`
(оставить `padding`/`margin-top` как есть; `.stream-block-think` на строке 206 — без изменений).

- [ ] **Step 6: Тесты зелёные**

Run: `uv run pytest tests/tui/test_render_stream_answer.py tests/tui/test_select_stream_render.py -v`
Expected: PASS (7 + 6).

- [ ] **Step 7: Commit**

```bash
git add src/tui/services/markdown_render.py src/tui/services/stream_sink.py src/tui/widgets/stream_block.py src/tui/styles.tcss tests/tui/test_render_stream_answer.py tests/tui/test_select_stream_render.py
git commit -m "feat(tui): render_stream_answer (INK inline V1) + select_stream_render + StreamBlock renderable"
```

---

### Task 3: Связать в `_poll_active` + мигрировать интеграционные тесты на новый флаг

**Files:**
- Modify: `src/tui/screens/main.py:1512-1535` (импорты + switch по `tui_stream_display`)
- Modify: `tests/tui/test_stream_block_render.py` (8 конфигов старых флагов → `tui_stream_display`)

**Interfaces:**
- Consumes: `select_stream_render`, `render_stream_answer` (Task 2); `format_stream_block`/`format_reasoning_line` (stream_sink, существуют); `tui_stream_display` (Task 1).
- Шов не трогаем: `_render_event` → `_clear_stream_block()` (main.py:804) уже снимает блок при монтаже канонической строки.

- [ ] **Step 1: Мигрировать конфиги в существующих тестах (RED)**

В `tests/tui/test_stream_block_render.py` заменить в `config={...}` каждого теста:
- `"tui_show_stream_answer": True` → `"tui_stream_display": "answer"` (тесты `test_stream_block_mounts_then_clears_on_canonical_row` ~L70, `test_reset_render_state_clears_stream_block_and_sink` ~L113, `test_stream_block_cleared_when_run_not_running` ~L147);
- `"tui_show_reasoning_line": True` → `"tui_stream_display": "answer_reasoning"` (`test_reasoning_line_below_committed_rows` ~L182, `test_reasoning_block_cleared_when_tool_row_renders` ~L241);
- `"tui_show_stream_answer": False` → `"tui_stream_display": "minimal"` (`test_stream_answer_preview_false_hides_answer_block` ~L316);
- `"tui_show_stream_answer": False, "tui_show_reasoning_line": True` → `"tui_stream_display": "answer_reasoning"` (`test_stream_answer_preview_false_shows_reasoning_line` ~L352);
- `"tui_show_reasoning_line": False` → `"tui_stream_display": "answer"` (`test_show_reasoning_line_false_hides_reasoning_block` ~L387).

(Имена/ассерты тестов не трогаем — меняется только значение конфига; при наличии inline-комментария с именем старого флага — поправить на `tui_stream_display`.)

- [ ] **Step 2: Запустить — упадёт**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: FAIL — `_poll_active` ещё читает старые ключи (`tui_show_*`), новый `tui_stream_display` игнорируется → блоки не появляются/появляются не там.

- [ ] **Step 3: Переписать стрим-ветку `_poll_active`**

В `src/tui/screens/main.py` заменить блок строк 1511-1535 (от `if _thinking_phase and sink is not None and sink.has_content():` до `self._clear_stream_block()` включительно) на:

```python
            if _thinking_phase and sink is not None and sink.has_content():
                from tui.services.stream_sink import (
                    format_stream_block, format_reasoning_line, select_stream_render,
                )
                from tui.services.markdown_render import render_stream_answer
                from tui.widgets.stream_block import StreamBlock
                think, answer = sink.snapshot()
                _cfg = getattr(self.app, "_config", None) or {}
                mode = _cfg.get("tui_stream_display", "answer")
                clean = format_stream_block(answer=answer)
                reasoning = format_reasoning_line(think=think)
                kind, thinking = select_stream_render(mode, clean, reasoning)
                if kind == "answer":
                    renderable = render_stream_answer(clean)
                elif kind == "reasoning":
                    renderable = reasoning
                else:
                    renderable = None
                if renderable is not None:
                    if self._stream_block is None:
                        self._stream_block = StreamBlock()
                        self._trace_mount(self._stream_block, scroll=True)
                    self._stream_block.set_text(renderable, thinking=thinking)
                else:
                    self._clear_stream_block(reset_sink=False)
            elif not _thinking_phase:
                self._clear_stream_block()
```

- [ ] **Step 4: Тесты зелёные**

Run: `uv run pytest tests/tui/test_stream_block_render.py -v`
Expected: PASS (все). При падении `test_stream_block_mounts_then_clears_on_canonical_row` на сравнении текста — проверить, что `str(blocks[0].render())` для прозы «Это проект.» равно plain-тексту (V1 стилизует, но `.plain` не меняет содержимое).

- [ ] **Step 5: Регресс остального TUI-стрима + чистота**

Run: `uv run pytest tests/tui/test_stream_sink.py tests/tui/test_app_stream_wiring.py tests/tui/test_stream_block_thinking_class.py -q`
Expected: PASS.

Run: `grep -rn "tui_show_stream_answer\|tui_show_reasoning_line" src/ tests/`
Expected: пусто.

- [ ] **Step 6: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_stream_block_render.py
git commit -m "feat(tui): _poll_active рендерит ответ прогрессивно по tui_stream_display (V1)"
```

---

### Task 4: Финальная сверка

- [ ] **Step 1: Прогнать весь затронутый набор**

Run: `uv run pytest tests/test_tui_config.py tests/tui/test_render_stream_answer.py tests/tui/test_select_stream_render.py tests/tui/test_stream_block_render.py tests/tui/test_stream_sink.py tests/tui/test_app_stream_wiring.py -q`
Expected: PASS.

- [ ] **Step 2: Доки спеки/плана (опц.)**

```bash
git add docs/tui/specs/2026-06-26-tui-progressive-stream-answer-design.md docs/tui/plans/2026-06-26-tui-progressive-stream-answer.md
git commit -m "docs(tui): дизайн+план — прогрессивный показ ответа (V1) + true-parity Direction A"
```

---

## Self-Review

**Spec coverage:**
- §1 флаг-модель (enum, default, clean break) → Task 1 (схема + flatten + конфиги + доки + тесты). ✓
- §2 рендер V1 (`render_stream_answer` в markdown_render; `select_stream_render` в stream_sink; `<think>`-strip в `format_stream_block`; StreamBlock renderable; `.stream-block` $ink) → Task 2. ✓
- §3 switch по `tui_stream_display` в `_poll_active` → Task 3 Step 3. ✓
- §4 шов — нового кода нет (main.py:804), покрыт `test_stream_block_mounts_then_clears_on_canonical_row` → Task 3 (миграция конфигов сохраняет покрытие). ✓
- §6 тесты: `render_stream_answer`/`select_stream_render` → Task 2; режим→поведение (minimal/answer/answer_reasoning) → существующие интеграционные тесты, мигрированные в Task 3. ✓
- Варианты роста (V2 / true-parity Direction A) — вне scope, не реализуем. ✓

**Placeholder scan:** заглушек нет — код и точные команды приведены.

**Type consistency:** `render_stream_answer(str)->Text`, `select_stream_render(str,str,str)->tuple[str,bool]` (kind ∈ {"answer","reasoning","none"}) — имена/типы согласованы между Task 2 и Task 3; `tui_stream_display` (один ключ) согласован Task 1↔Task 3; `format_stream_block`/`format_reasoning_line` — существующие, не переименовываются.
