# Унификация имени файлов-инструкций (везде AGENTS.md) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать имя всех пользовательских файлов-инструкций (L1/L2/L3) единым — `AGENTS.md`, различая слой по расположению; убрать `GIGACODE.md` как имя файла-инструкции без fallback.

**Architecture:** Слои инструкций определяются в `src/instructions/layers.py` через константу `_BRAND` (имя для L1 и L3) и литерал `AGENTS.md` (для L2). Достаточно сменить `_BRAND` на `"AGENTS.md"` — пути L1 (`~/.gc/AGENTS.md`) и L3 (`.gc/AGENTS.md`) переименуются автоматически; `loader.py`, `protected_paths()` и `states.py` потребляют это без правок. Остальное — синхронизация текстов (help, комментарии конфигов, changelog).

**Tech Stack:** Python ≥ 3.10, pytest. Абсолютные импорты от `src/` (`conftest.py` кладёт `src/` в `sys.path`).

## Global Constraints

- Импорты — абсолютные от `src/` (`from instructions.layers import ...`), не относительные, не `from src...`.
- `GIGACODE.md` как имя файла-инструкции — **без fallback** (старые файлы намеренно перестают читаться).
- Правки `config`-комментариев отражать синхронно в `demo_config.yml` **и** `config.default.yml`.
- Бренд `GIGACODE` вне файла-инструкции (`src/tui/branding.py`, install-страницы, long-memory, имя продукта «GigaCode») — **не трогать**.
- Исторические спеки `docs/specs/2026-06-20-*` — не переписывать.
- Коммит-сообщения лаконичные, без AI-атрибуции и имён.

---

### Task 1: Переименование ядра слоёв L1/L3 на AGENTS.md

**Files:**
- Modify: `src/instructions/layers.py:9` (константа `_BRAND`), `src/instructions/layers.py:44` (заголовок L3)
- Test: `tests/instructions/test_layers.py:26`, `tests/instructions/test_loader.py:16,18,36`, `tests/security/test_protected_paths.py:22-23`

**Interfaces:**
- Consumes: ничего нового.
- Produces: `instruction_file_paths(cwd=...)["L1"] == <gc_home>/AGENTS.md`, `["L3"] == <cwd>/.gc/AGENTS.md`; маркер-заголовок L3 в собранном тексте — `## Project instructions (.gc/AGENTS.md)`. Поведение (склейка, приоритет L0<L1<L2<L3, `overwrite`→только проектные, классификация в `protected_paths().instructions`) не меняется.

- [ ] **Step 1: Обновить тесты на новое имя (RED)**

В `tests/instructions/test_layers.py` строка 26 — заменить:
```python
    assert paths["L3"] == (tmp_path / "proj" / ".gc" / "AGENTS.md")
```

В `tests/instructions/test_loader.py` заменить три строки записи файлов L1/L3 (строки 16, 18, 36):
```python
    _write((tmp_path / "gc") / "AGENTS.md", "L1BODY")
```
(строка 18:)
```python
    _write(proj / ".gc" / "AGENTS.md", "L3BODY")
```
(строка 36:)
```python
    _write((tmp_path / "gc") / "AGENTS.md", "L1BODY")
```

В том же `test_loader.py`, в `test_merges_broad_to_narrow_with_markers`, добавить после строки 27 проверку маркера L3:
```python
    assert "## Project instructions (.gc/AGENTS.md)" in res.text
```

В `tests/security/test_protected_paths.py` строки 22-23 — заменить:
```python
    # L1 AGENTS.md лежит в gc config-каталоге, но это класс инструкций, не операционный
    assert any(p.name == "AGENTS.md" for p in ps.instructions)
```

- [ ] **Step 2: Запустить тесты — убедиться, что падают (RED)**

Run: `pytest tests/instructions/test_layers.py tests/instructions/test_loader.py tests/security/test_protected_paths.py -v`
Expected: FAIL — `test_file_paths_excludes_L0` (ждёт `.gc/AGENTS.md`, получает `.gc/GIGACODE.md`), `test_merges_broad_to_narrow_with_markers` (нет файлов `AGENTS.md` в gc/.gc → `loaded_ids` без L1/L3; и нет маркера L3), `test_instruction_files_not_in_operational` (нет файла с именем `AGENTS.md` среди gc-инструкций → реально упадёт на отсутствии `GIGACODE.md`).

- [ ] **Step 3: Сменить `_BRAND` и заголовок L3 (GREEN)**

В `src/instructions/layers.py` строка 9 — заменить:
```python
_BRAND = "AGENTS.md"
```

Строка 44 — заменить:
```python
    specs.append(LayerSpec("L3", "Project instructions (.gc/AGENTS.md)", files["L3"], "project"))
```

- [ ] **Step 4: Запустить тесты — убедиться, что проходят (GREEN)**

Run: `pytest tests/instructions/test_layers.py tests/instructions/test_loader.py tests/security/test_protected_paths.py -v`
Expected: PASS (все).

- [ ] **Step 5: Прогнать смежные тесты слоёв/состояния (регрессия)**

Run: `pytest tests/instructions tests/security/test_protected_paths.py tests/test_prompt_fallback.py -v`
Expected: PASS — поведение склейки/приоритета/overwrite не изменилось, L0-fallback (`demo_cli_agent.md`) не затронут.

- [ ] **Step 6: Commit**

```bash
git add src/instructions/layers.py tests/instructions/test_layers.py tests/instructions/test_loader.py tests/security/test_protected_paths.py
git commit -m "feat(instructions): единое имя AGENTS.md для слоёв L1/L3 (убран GIGACODE.md)"
```

---

### Task 2: Синхронизация текстов (help, комментарии конфигов, changelog)

**Files:**
- Modify: `src/arg_parser.py:221`, `demo_config.yml:134`, `config.default.yml:65`, `docs/CHANGES-vs-master.md:40,42`

**Interfaces:**
- Consumes: новое имя `AGENTS.md` из Task 1.
- Produces: пользовательские тексты согласованы с моделью «везде AGENTS.md». Кода/тестов не затрагивает.

- [ ] **Step 1: Поправить help-текст CLI-флага**

В `src/arg_parser.py` строка 221 — заменить:
```python
        "Загружать только проектные слои инструкций (AGENTS.md + .gc/AGENTS.md), "
```

- [ ] **Step 2: Поправить комментарий в двух конфигах синхронно**

В `demo_config.yml` строка 134 и `config.default.yml` строка 65 — заменить (одинаково в обоих):
```yaml
  # instructions_overwrite: false  # загружать только проектные слои (AGENTS.md + .gc/AGENTS.md), отбрасывая глобальный и shipped
```

- [ ] **Step 3: Обновить описание модели в changelog**

В `docs/CHANGES-vs-master.md`:

Строку 40 (ячейка описания слоёв L0–L3) — заменить текст про два имени и некоммитимость `.gc/` на единую модель. Новое содержимое ячейки «Реализация»:
```
Слои L0–L3, merge broad→narrow: shipped → глоб. `~/.gc/AGENTS.md` → проектный `AGENTS.md` → проектный `.gc/AGENTS.md`; склейка с английским scaffold + преамбула приоритета. Единое имя `AGENTS.md`, слой определяется расположением (открытый кросс-тул стандарт; commit-политику решает пользователь). Приоритет L0<L1<L2<L3.
```

Строку 42 (ячейка про `instructions_overwrite`) — заменить `оставить AGENTS.md + .gc/GIGACODE.md` на:
```
оставить `AGENTS.md` + `.gc/AGENTS.md`, отбросить глобальный/shipped
```

- [ ] **Step 4: Проверить, что `GIGACODE.md` как файл-инструкция вычищен**

Run: `git grep -n "GIGACODE.md" -- src tests demo_config.yml config.default.yml docs/CHANGES-vs-master.md`
Expected: пусто (ноль совпадений). Бренд `GIGACODE` без `.md` и исторические спеки `docs/specs/2026-06-20-*` сюда не входят и остаются как есть.

- [ ] **Step 5: Финальный прогон тестов**

Run: `pytest tests/instructions tests/security/test_protected_paths.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/arg_parser.py demo_config.yml config.default.yml docs/CHANGES-vs-master.md
git commit -m "docs: синхронизация текстов под единое имя AGENTS.md"
```

---

## Self-Review

**1. Spec coverage:**
- Решение «везде AGENTS.md, scope по директории» → Task 1 (`_BRAND`).
- Заголовок L3 без «personal» → Task 1 Step 3.
- Help-текст / комментарии конфигов / changelog → Task 2.
- Breaking change без fallback → не пишем fallback-код; зафиксировано в changelog (Task 2 Step 3) и в спеке.
- L0 не трогаем → ни одна задача его не меняет; регрессия проверяется `test_prompt_fallback.py` (Task 1 Step 5).
- Гитигнор не добавляем → нет соответствующей задачи (намеренно).
- `init` без изменений → нет задачи (намеренно).
- Тесты `test_layers/test_loader/test_protected_paths` → Task 1.
- Критерий «GIGACODE.md как файл не встречается в src/тестах/активных конфигах» → Task 2 Step 4 (`git grep`).

**2. Placeholder scan:** плейсхолдеров нет — все шаги содержат конкретный код/команды/ожидаемый вывод.

**3. Type consistency:** имена `_BRAND`, `instruction_file_paths`, `instruction_layer_specs`, `LayerSpec`, `build_instructions`, `protected_paths`, поля `loaded_ids`/`text`/`instructions` совпадают с исходным кодом, прочитанным при составлении плана.
