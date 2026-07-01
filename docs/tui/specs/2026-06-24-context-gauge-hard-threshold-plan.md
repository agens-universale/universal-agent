# Context gauge hard-threshold — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Нормировать индикатор «контекст NN%» на жёсткий порог форс-сжатия (`summary_trigger`), а не на turn-end — чтобы бар не залипал на 100% задолго до сжатия.

**Architecture:** Одна строка в `_context_meter`: жёсткий порог primary, turn-end fallback (`parse_summary_trigger_chars(cfg) or parse_summary_turn_end_trigger_chars(cfg)`) + корректный комментарий. Плюс screen-level тест выбора порога (сейчас не покрыт). Pipeline/пороги/чистая `render_context_meter`/конфиг — не трогаем.

**Tech Stack:** Python ≥3.10, Textual, pytest (`uv run pytest`), абсолютные импорты от `src/`.

## Global Constraints

- Правка только в `src/tui/screens/main.py::_context_meter` (выбор порога + комментарий) + новый тест-файл.
- НЕ трогать: `src/tui/services/context_meter.py` (`render_context_meter`), `src/run_summary_coordinator.py` и summary-pipeline, пороги сжатия и ratio 0.6, конфиг, `prompts/chat_completions_system_prompt.md`.
- Импорт обеих `parse_summary_trigger_chars` и `parse_summary_turn_end_trigger_chars` остаётся (обе используются).
- Абсолютные импорты от `src/`. Тесты — pytest, прямые `assert`, запуск `uv run pytest`.
- Коммит-сообщения: лаконично, по-русски, без имён коллег и AI-атрибуции.

---

### Task 1: Гейдж нормировать на жёсткий порог + screen-level тест

**Files:**
- Modify: `src/tui/screens/main.py` (метод `_context_meter`, ~строки 209–229)
- Test: `tests/tui/test_context_meter_threshold.py` (создать)

**Interfaces:**
- Consumes: `summarize.config.parse_summary_trigger_chars(cfg) -> int`, `parse_summary_turn_end_trigger_chars(cfg) -> int`; `TraceScreen._context_meter() -> tuple[str,str] | None`; `TraceScreen.attach_active_task(tid)`.
- Produces: ничего нового для других задач (план из одной задачи).

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/tui/test_context_meter_threshold.py`:

```python
"""_context_meter нормирует бар на ЖЁСТКИЙ порог (summary_trigger), а не turn-end."""
import json
import pytest


def _write_task(tmp_path, tid, content):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    rec = {"type": "message", "thread_id": tid, "system": "cli", "seq": 1, "ts": "t",
           "payload": {"role": "user", "content": content}}
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _cfg(trigger, turn_end):
    return {"model": "M", "backend_api_mode": "chat_completions",
            "summary_params": {"chat_completions_summary_trigger_chars": trigger,
                               "chat_completions_summary_turn_end_trigger_chars": turn_end}}


@pytest.mark.asyncio
async def test_meter_uses_hard_threshold_not_turn_end(tmp_path):
    """Бар нормируется от summary_trigger (10M), а не turn-end (1) → ~0%, не 100%."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "ctxm0000000000000000000000000001a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config=_cfg(trigger=10_000_000, turn_end=1))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert meter is not None
        text, _ = meter
        assert "0%" in text   # summary 10M в знаменателе; на старом коде (turn_end=1) было бы 100%


@pytest.mark.asyncio
async def test_meter_falls_back_to_turn_end_when_no_hard_threshold(tmp_path):
    """summary_trigger=0 (форс-сжатие выкл) → fallback на turn-end, бар виден."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "ctxm0000000000000000000000000002a"
    _write_task(tmp_path, tid, "hello")
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config=_cfg(trigger=0, turn_end=10_000_000))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        meter = scr._context_meter()
        assert meter is not None   # fallback на turn_end (иначе порог 0 → None/скрыт)
        text, _ = meter
        assert "%" in text
```

- [ ] **Step 2: Запустить — убедиться, что test 1 падает**

Run: `uv run pytest tests/tui/test_context_meter_threshold.py -q`
Expected: `test_meter_uses_hard_threshold_not_turn_end` FAIL (текущий код берёт turn_end=1 primary → бар «100%», а тест ждёт «0%»). `test_meter_falls_back...` может уже проходить (на старом коде `turn_end=10M or 0` тоже даёт 10M) — это нормально.

- [ ] **Step 3: Поменять порог + комментарий**

В `src/tui/screens/main.py`, в методе `_context_meter`, заменить блок (комментарий + import + строка порога):

`old_string`:
```python
        # Калибруем гейдж на ОПЕРАЦИОННЫЙ порог сжатия — turn-end (срабатывает первым,
        # после каждого tool-output), а не на pre-run summary_trigger. Иначе гейдж считал
        # бы от полного summary_trigger, а сжатие стартует при 60% (turn-end) → выглядело
        # бы как «сжатие при ~60-77%». turn-end не задан → fallback на summary_trigger.
        from summarize.config import (
            parse_summary_trigger_chars,
            parse_summary_turn_end_trigger_chars,
        )
        threshold = parse_summary_turn_end_trigger_chars(cfg) or parse_summary_trigger_chars(cfg)
```

`new_string`:
```python
        # Калибруем гейдж на ЖЁСТКИЙ порог форс-сжатия — summary_trigger (по нему жмут
        # pre_backend мид-турн и pre-run). turn-end (ratio 0.6) — ПРОАКТИВНАЯ уборка
        # только на границе хода (turn_finish), её показывает нижняя плашка «сжимаю…»,
        # а не 100% на баре. summary_trigger=0 (форс-сжатие выключено) → fallback на
        # turn-end как на единственный реальный порог.
        from summarize.config import (
            parse_summary_trigger_chars,
            parse_summary_turn_end_trigger_chars,
        )
        threshold = parse_summary_trigger_chars(cfg) or parse_summary_turn_end_trigger_chars(cfg)
```

(Меняется текст комментария и порядок в `or`; импорт обеих функций сохраняется.)

- [ ] **Step 4: Запустить — убедиться, что оба теста проходят**

Run: `uv run pytest tests/tui/test_context_meter_threshold.py -q`
Expected: PASS (оба).

- [ ] **Step 5: Регресс**

Run: `uv run pytest tests/tui/test_context_meter.py tests/tui/test_context_meter_threshold.py -q`
Expected: PASS. (Чистая `render_context_meter` не менялась → `test_context_meter.py` зелёный.) Если в наборе есть снапшот-тест шапки, где % индикатора попал в снапшот и теперь сдвинулся — регенерировать снапшот осознанно (`--snapshot-update`) и отметить в отчёте; смысловой регрессии тут нет.

- [ ] **Step 6: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_context_meter_threshold.py
git commit -m "fix(tui): гейдж контекста меряет до жёсткого порога сжатия (summary_trigger), не turn-end"
```

---

## Self-Review

**1. Spec coverage:**
- «Порядок порога: summary_trigger primary, turn-end fallback» → Task 1 Step 3 (точный old/new). ✓
- «Поправить ошибочный комментарий» → Step 3 (new_string). ✓
- «Импорт обеих функций остаётся» → Step 3 (импорт в old и new идентичен). ✓
- «render_context_meter/pipeline/пороги/конфиг/системный промпт не трогать» → Global Constraints + единственный изменяемый файл. ✓
- «Screen-level тест выбора порога (не покрыт)»: test 1 (hard-threshold, RED→GREEN) + test 2 (fallback) → Step 1. ✓
- «Регресс test_context_meter.py + снапшоты шапки» → Step 5. ✓
- Trade-off (turn-end-сжатие на 60–100%) — поведенческое следствие, отдельной задачи не требует. ✓

**2. Placeholder scan:** Плейсхолдеров нет — код тестов и обе строки правки приведены дословно, команды и ожидаемые результаты конкретны. ✓

**3. Type consistency:** `_context_meter() -> tuple|None`, `parse_summary_*-> int`, `attach_active_task(tid)` — используются согласованно; имена тестов уникальны. ✓
