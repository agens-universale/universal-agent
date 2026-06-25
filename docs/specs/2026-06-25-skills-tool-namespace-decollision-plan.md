# Skills-тулы: устранение префиксной коллизии — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Переименовать `skills_load_reference`→`skills_read_reference` и `skills_load_asset`→`skills_read_asset`, чтобы ни одно skills-тул-имя не было строгим префиксом другого и слабая модель снова могла достигать `skills_load`.

**Architecture:** Чистое переименование двух строковых токенов во всех точках их появления (определения тулов + envelope + кросс-ссылки в описаниях; текст доставки скилла; обучающий контент репо-скиллов). Без алиасов, без миграции истории, без правок главного цикла. Драйвер — новый регресс-тест на отсутствие префиксного контейнмента.

**Tech Stack:** Python ≥3.10, pytest. Абсолютные импорты от `src/` (`conftest.py` кладёт `src/` в path).

Спека: [docs/specs/2026-06-25-skills-tool-namespace-decollision-design.md](2026-06-25-skills-tool-namespace-decollision-design.md)

## Global Constraints

- Финальный набор skills-тул-имён: `skills_list`, `skills_load`, `skills_run_script`, `skills_read_reference`, `skills_read_asset`. Ни одно имя не является строгим префиксом другого.
- `skills_load` / `skills_list` / `skills_run_script` — НЕ трогать.
- Без алиасов старых имён; миграция тредов не нужна.
- Точные замены (везде, дословно): `skills_load_reference` → `skills_read_reference`; `skills_load_asset` → `skills_read_asset`.
- Импорты — абсолютные от `src/` (`from tools.skills... import ...`).
- Коммиты — лаконичные, по-русски/смешанно по окружению; без AI-атрибуции, без упоминаний Claude/Anthropic/моделей. НЕ коммитить `.claude/`, `CLAUDE.md`.
- Линтера в проекте нет — вслепую не запускать.

---

### Task 1: Переименование в определениях тулов + регресс на контейнмент

**Files:**
- Create: `tests/skills/test_tool_name_no_prefix_collision.py`
- Modify: `src/tools/skills/skills_tools.py` (все вхождения двух токенов: envelope `tool=` в `LoadReferenceTool`/`LoadAssetTool`, ключи словаря, `spec.name`, тексты описаний, кросс-ссылки в описании `skills_load`)

**Interfaces:**
- Produces: skills-тулы под именами `skills_read_reference`, `skills_read_asset` (ключи `tools_data`, `spec.name`, `tool`-поле envelope). Классы Python (`LoadReferenceTool`, `LoadAssetTool`) НЕ переименовываются — меняются только строковые имена тулов.
- Consumes: `get_skills_tools_data(skills_registry)` — на этапе построения словаря `skills_registry` не разыменовывается (используется только внутри фабрик), поэтому в тесте допустим вызов `get_skills_tools_data(None)`.

- [ ] **Step 1: Написать падающий тест**

Create `tests/skills/test_tool_name_no_prefix_collision.py`:

```python
"""Регресс: ни одно skills-тул-имя не должно быть строгим префиксом другого.

Слабая модель при `tool_choice: auto`, набрав длинное имя тула в истории, не
может автогрессивно остановиться на более коротком имени-префиксе — она
дописывает виденный суффикс. `skills_load` ⊂ `skills_load_reference` — ровно эта
ловушка. См. docs/specs/2026-06-25-skills-tool-namespace-decollision-design.md.
"""
from tools.skills.skills_tools import get_skills_tools_data


def test_skills_tool_names_have_no_prefix_collision():
    names = list(get_skills_tools_data(None).keys())
    collisions = [
        (a, b)
        for a in names
        for b in names
        if a != b and b.startswith(a)
    ]
    assert not collisions, (
        "tool name is a strict prefix of another (autoregressive tool-name trap): "
        + ", ".join(f"{a!r} prefix-of {b!r}" for a, b in collisions)
    )
```

- [ ] **Step 2: Запустить тест — убедиться, что падает**

Run: `pytest tests/skills/test_tool_name_no_prefix_collision.py -v`
Expected: FAIL — `'skills_load' prefix-of 'skills_load_reference'` (и `'skills_load' prefix-of 'skills_load_asset'`).

- [ ] **Step 3: Переименовать токены в `skills_tools.py`**

В `src/tools/skills/skills_tools.py` заменить ВСЕ вхождения (replace_all):
- `skills_load_reference` → `skills_read_reference`
- `skills_load_asset` → `skills_read_asset`

Это покрывает: envelope `tool=` (строки ~215–251, ~263–299), кросс-ссылки в описании `skills_load` (~362, ~386), ключи словаря (~453, ~503), `spec.name` (~457, ~507), тексты описаний (~488, ~538). `logger.error("... failed")` строки тоже переименуются — это ок.

- [ ] **Step 4: Запустить тест на контейнмент — должен пройти**

Run: `pytest tests/skills/test_tool_name_no_prefix_collision.py -v`
Expected: PASS.

- [ ] **Step 5: Прогнать существующие тесты пакетов (sanity)**

Run: `pytest tests/test_skills_tools_packs.py -v`
Expected: PASS — эти тесты проверяют `error.code` (`AMBIGUOUS_SKILL_ID`) и не зависят от строкового имени тула; имена тест-функций (`test_skills_load_reference_ambiguous...`) оставляем как есть (переименование имён тест-функций — лишний churn, поведение не меняет).

- [ ] **Step 6: Коммит**

```bash
git add tests/skills/test_tool_name_no_prefix_collision.py src/tools/skills/skills_tools.py
git commit -m "fix(skills): убрать префиксную коллизию имён тулов — skills_load_{reference,asset} -> skills_read_{reference,asset}"
```

---

### Task 2: Текст доставки скилла на новые имена

**Files:**
- Modify: `tests/skills/test_skill_delivery.py` (строки 25, 39, 48, 55)
- Modify: `src/tools/skills/skill_delivery.py` (вхождения двух токенов: ~строки 34, 41, 51)

**Interfaces:**
- Consumes: `build_skill_delivery(skill_id, registry)` — текст доставки; после правки содержит `skills_read_reference` / `skills_read_asset` вместо старых имён.

- [ ] **Step 1: Обновить ожидания в тесте доставки (это даст RED против текущего кода)**

В `tests/skills/test_skill_delivery.py`:
- L25: `assert "skills_load_reference" in out` → `assert "skills_read_reference" in out`
- L39: `assert "skills_load_reference" not in out` → `assert "skills_read_reference" not in out`
- L48: `assert "skills_load_asset" in out` → `assert "skills_read_asset" in out`
- L55: `assert "skills_load_reference" not in out` → `assert "skills_read_reference" not in out`

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/skills/test_skill_delivery.py -v`
Expected: FAIL на `test_injects_instructions_refs_and_lock` и `test_assets_listed_when_present` — доставка всё ещё эмитит старые имена (`assert "skills_read_reference" in out` не находит).

- [ ] **Step 3: Переименовать токены в `skill_delivery.py`**

В `src/tools/skills/skill_delivery.py` заменить ВСЕ вхождения (replace_all):
- `skills_load_reference` → `skills_read_reference`
- `skills_load_asset` → `skills_read_asset`

(Это строки императива «грузи references через …» и «грузи assets через …», плюс совместная клауза замка.)

- [ ] **Step 4: Запустить тест доставки — должен пройти**

Run: `pytest tests/skills/test_skill_delivery.py -v`
Expected: PASS (все 8 тестов).

- [ ] **Step 5: Коммит**

```bash
git add tests/skills/test_skill_delivery.py src/tools/skills/skill_delivery.py
git commit -m "fix(skills): доставка скилла ссылается на skills_read_{reference,asset}"
```

---

### Task 3: Обучающий контент репо-скиллов + докстринг tool-agnostic теста

**Files:**
- Modify: `skills/supercoder/writing-skills/SKILL.md` (строки 81–82, 101)
- Modify: `skills/supercoder/systematic-debugging/SKILL.md` (строки 39, 64, 66, 68)
- Modify: `skills/supercoder/test-driven-development/SKILL.md` (строки 30, 68)
- Modify: `skills/skill-creator/SKILL.md` (строки 546, 552, 673)
- Modify: `skills/skill-creator/assets/completion_report_template.md` (строка 4)
- Modify: `tests/test_skills_tool_agnostic.py` (докстринг, строка 7)

**Interfaces:**
- Чисто текстовые правки. `test_no_runtime_tool_names` остаётся зелёным: имена `skills_read_*` не входят в `FORBIDDEN_RUNTIME_TOOLS` (как и старые `skills_load_*` — это разрешённые имена механизма скиллов).

- [ ] **Step 1: Заменить токены во всех пяти md-файлах скиллов**

В каждом из файлов выше заменить ВСЕ вхождения:
- `skills_load_reference` → `skills_read_reference`
- `skills_load_asset` → `skills_read_asset`

- [ ] **Step 2: Обновить докстринг tool-agnostic теста**

В `tests/test_skills_tool_agnostic.py` строки 6–7 — в перечне разрешённых имён механизма скиллов:
`` ``skills_load_reference``, ``skills_load_asset`` `` → `` ``skills_read_reference``, ``skills_read_asset`` ``.

- [ ] **Step 3: Проверить, что старых токенов в `skills/` не осталось, и тест зелёный**

Run: `pytest tests/test_skills_tool_agnostic.py -v`
Expected: PASS.

Run (grep по репо-скиллам — должно быть пусто):
`grep -rn "skills_load_reference\|skills_load_asset" skills/`
Expected: нет вывода.

- [ ] **Step 4: Коммит**

```bash
git add skills/ tests/test_skills_tool_agnostic.py
git commit -m "docs(skills): обучающий контент и tool-agnostic guard на skills_read_{reference,asset}"
```

---

### Task 4: Финальная сверка по всему репо

- [ ] **Step 1: Убедиться, что в `src/` и `tests/` не осталось старых имён**

Run: `grep -rn "skills_load_reference\|skills_load_asset" src/ tests/`
Expected: единственные допустимые вхождения — имена тест-функций в `tests/test_skills_tools_packs.py` (намеренно не переименованы, Task 1 Step 5). Любые другие — устранить тем же replace.

- [ ] **Step 2: Прогнать релевантный набор тестов**

Run: `pytest tests/skills/ tests/test_skills_tools_packs.py tests/test_skills_tool_agnostic.py -v`
Expected: PASS.

- [ ] **Step 3 (опц.): зафиксировать спеку и план в git**

```bash
git add docs/specs/2026-06-25-skills-tool-namespace-decollision-design.md docs/specs/2026-06-25-skills-tool-namespace-decollision-plan.md
git commit -m "docs(spec): дизайн+план — устранение префиксной коллизии имён skills-тулов"
```

---

## Self-Review

**Spec coverage:**
- Переименование `skills_tools.py` (имена/spec.name/envelope/кросс-ссылки) → Task 1.
- Без алиасов → отражено в Global Constraints + Architecture (механизм алиасов отсутствует).
- `skill_delivery.py` → Task 2.
- Репо-скиллы → Task 3.
- Тесты: регресс на контейнмент → Task 1; ожидания канон-имён (докстринг agnostic) → Task 3; доставка → Task 2. (Спека упоминала `test_skills_tools_packs.py` «ожидания на новые имена» — по факту там нет assert'ов на строковое имя тула, только `error.code`; зафиксировано в Task 1 Step 5 как намеренный no-op.)

**Placeholder scan:** заглушек нет — все замены и код приведены дословно.

**Type/name consistency:** во всех тасках одни и те же финальные имена `skills_read_reference` / `skills_read_asset`; классы Python не переименовываются; `get_skills_tools_data(None)` обоснован в Task 1 Interfaces.
