# Унификация имени файлов-инструкций: везде `AGENTS.md`

Дата: 2026-06-28
Статус: дизайн (на ревью)
Предшественник: [2026-06-20-layered-agent-instructions-and-init-design.md](2026-06-20-layered-agent-instructions-and-init-design.md)

## Проблема

Слои пользовательских инструкций сейчас используют **два разных имени файла**:

- L1 (глобальный): `~/.gc/GIGACODE.md`
- L2 (проектный общий): `AGENTS.md` (корень репо)
- L3 (проектный): `.gc/GIGACODE.md`

Расхождение `GIGACODE.md` / `AGENTS.md` путает: непонятно, что куда писать и что главнее. Исходная мотивация была «нейтральное `AGENTS.md` для кросс-агентного слоя + брендовый `GIGACODE.md` для нашего», но имя файла — слабая брендовая поверхность, а двойственность имён создаёт основную боль.

## Ключевой инсайт

Бренд GigaCode уже живёт в **директории** (`~/.gc/`, `.gc/`) и в самом продукте. `GIGACODE` в имени дотфайла — избыточное брендирование, которое и порождает путаницу. Значит имя файла можно сделать **одно — стандартное `AGENTS.md`**, а слой различать **по расположению**.

## Решение

Единое правило: **имя файла всегда `AGENTS.md`; слой определяется директорией.**

| Слой | Файл | Расположение | Опционален | Кто читает |
|---|---|---|---|---|
| L0 (база продукта) | `agent_md_path` (по умолч. `prompts/demo_cli_agent.md`) | из конфига | нет (всегда есть; подменяется в server/swarm) | только мы |
| L1 (глобальный) | `AGENTS.md` | `~/.gc/` (или `~/.config/gc/`) | да | только мы |
| L2 (проектный общий) | `AGENTS.md` | корень проекта | да | кросс-агентный стандарт |
| L3 (проектный GigaCode) | `AGENTS.md` | `.gc/` проекта | да | только мы |

- **Приоритет:** `L0 < L1 < L2 < L3` (специфичнее перекрывает). Без изменений.
- **Склейка, не выбор:** найденные файлы конкатенируются секциями; отсутствующий — пропускается. Без изменений.
- **Преамбула приоритета** в `loader.py` — без изменений.
- **`instructions_overwrite: true`** — по-прежнему оставляет только проектные слои (L2 + L3). Без изменений.

### L0 остаётся как есть

L0 — это **не** `AGENTS.md`, а базовое определение агента из `paths.agent_md_path`. Это единственный канал, которым продуктовое определение агента (роль, `## Long memory protocol`) попадает в промпт; в server/swarm подменяется на `server_agent.md` / свой `agent.md`. L0 — самый нижний приоритет, который L1–L3 законно перекрывают. Эта правка его **не касается**.

### Что НЕ решаем (commit-политика)

Коммитить `.gc/AGENTS.md` или нет — решает пользователь своим `.gitignore`. Код к этому нейтрален: мы определяем слои и читаем их, политику коммита не навязываем (**гитигнор не добавляем**). В доке — лишь типичный сценарий-совет: корневой `AGENTS.md` обычно общий/кросс-агентный, `.gc/AGENTS.md` — обычно GigaCode-специфика; оба варианта (коммитить / прятать) валидны.

## Breaking change

Старые `~/.gc/GIGACODE.md` и `.gc/GIGACODE.md` **перестают читаться** (fallback намеренно не делаем). Миграция = ручное переименование в `AGENTS.md`. Зафиксировать в `docs/CHANGES-vs-master.md`.

## Изменения в коде

1. [src/instructions/layers.py:9](../../src/instructions/layers.py#L9): `_BRAND = "GIGACODE.md"` → `"AGENTS.md"`. Автоматически переименовывает пути L1 и L3.
2. [src/instructions/layers.py:44](../../src/instructions/layers.py#L44): заголовок-маркер L3 → `"Project instructions (.gc/AGENTS.md)"` (убрать «personal» — слой командный/коммитимый по смыслу).
3. [src/arg_parser.py:221](../../src/arg_parser.py#L221): help-текст `instructions_overwrite` → `(AGENTS.md + .gc/AGENTS.md)`.
4. [demo_config.yml:134](../../demo_config.yml#L134) и [config.default.yml:65](../../config.default.yml#L65): комментарий `.gc/GIGACODE.md` → `.gc/AGENTS.md` (синхронно).
5. [docs/CHANGES-vs-master.md:40,42](../CHANGES-vs-master.md): обновить описание модели слоёв (единое имя `AGENTS.md`, scope по директории; убрать утверждение про некоммитимость `.gc/`).

## Изменения в тестах

Обновить имя файла на `AGENTS.md`, поведение проверяется тем же:

- [tests/instructions/test_layers.py](../../tests/instructions/test_layers.py) — путь L3.
- [tests/instructions/test_loader.py](../../tests/instructions/test_loader.py) — L1/L3 файлы.
- [tests/security/test_protected_paths.py](../../tests/security/test_protected_paths.py) — имя `GIGACODE.md` в `ps.instructions`.

Проверяемые инварианты (без изменения поведения): склейка слоёв, приоритет L0<L1<L2<L3, `instructions_overwrite` → только проектные, `protected_paths()` относит инструкции в класс `instructions`.

## Out of scope (намеренно)

- `init` — без изменений (уже создаёт корневой `AGENTS.md`; промпт-шаблон тоже про `AGENTS.md`).
- Чтение чужих имён (`CLAUDE.md` / `GEMINI.md` / `.cursorrules`) — нет.
- Переименование внутреннего swarm `.gc/agent.md` — нет.
- Бренд `GIGACODE` вне файла-инструкции (`tui/branding.py`, install-страницы, long-memory) — не трогаем.
- Naming-нит `demo_cli_agent.md` («demo» звучит одноразово, хотя это боевой L0) — отдельная задача, не здесь.
- Исторические спеки `docs/specs/2026-06-20-layered-agent-instructions-and-init-{design,plan}.md` и `2026-06-20-gc-protection-and-command-guard-design.md` ссылаются на `GIGACODE.md` как на тогдашнее решение — намеренно НЕ переписываем (это запись истории); данная спека их замещает.

## Критерии готовности

- `GIGACODE.md` как имя файла-инструкции не встречается в `src/`, тестах, активных конфигах.
- L1 читается из `~/.gc/AGENTS.md`, L3 — из `.gc/AGENTS.md`; L2 корневой — без изменений.
- Все тесты `tests/instructions/*`, `tests/security/test_protected_paths.py` зелёные.
- Бренд-вхождения `GIGACODE` (не файл) не задеты.
