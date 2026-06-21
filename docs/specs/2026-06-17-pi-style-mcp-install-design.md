# Pi-style установка MCP-пакетов для gc-ua — дизайн (v1)

- **Дата:** 2026-06-17
- **Статус:** дизайн утверждён, готов к плану реализации
- **Инициатива:** follow-up из `docs/2026-06-16-pi-style-mcp-packages-followup.md` (лид: «нам бы как в Pi»)
- **Зависит от:** фикса упаковки (`feat/packaging-install`) — после него `recommended_mcp/` едет в wheel и `gc-mcp-install` работает в method-2. Реализуется отдельной веткой от master ПОСЛЕ мерджа того PR.

---

## 1. Контекст и проблема

Сейчас установка MCP — две раздельные ручные операции:
1. `gc-mcp-install` ставит пакеты MCP в отдельный venv (`<gc_data>/recommended_mcp/venv`);
2. чтобы агент **увидел** MCP, пользователь **вручную** копирует блок из `recommended_mcp/<name>/agent_config.snippet.yml` в секцию `mcp-servers:` своего `config.yml`.

«Поставил» ≠ «работает» — нужен ручной шаг копирования. README прямо фиксирует: «install.py не редактирует config.yml автоматически».

Лид восхитился Pi (`pi install npm:<pkg>` → пакет ставится и **сам** подключается). Цель — перенести **установочный UX** Pi, сохранив нашу модель (MCP-серверы по протоколу, отдельный venv) — **не** копируя in-process extension-модель Pi.

---

## 2. Цели и не-цели (v1)

**Цели:**
- `gc-mcp install <name>` — поставить один MCP по имени из курируемого манифеста **и сразу авто-подключить** (auto-wire) в конфиг агента.
- `gc-mcp list` — показать доступные / установленные / подключённые MCP.
- `gc-mcp remove <name>` — **полный снос**: отключить из конфига + деинсталлировать из venv.
- Конфиг пользователя (`~/.gc/config.yml`) при этом **не трогается** — wiring пишет в отдельный machine-managed файл.

**Не-цели (v2):**
- Произвольные источники: `gc-mcp install pypi:<pkg>` / `git:<url>` / `local:<path>` (v1 — только курируемый манифест).
- Сбор orphan-зависимостей из общего venv; GC кэша браузеров playwright (chromium).
- Браузабельный каталог/реестр; публикация MCP во внутренний индекс.
- In-process extension-модель Pi (наши «пакеты» остаются MCP-серверами).

---

## 3. Решения брейншторма (зафиксированы)

| # | Вопрос | Решение |
|---|---|---|
| 1 | Источники v1 | Только курируемый манифест (`recommended_mcp/config.yml`) по имени. Произвольные — v2. |
| 2 | Куда пишет auto-wire | Отдельный machine-managed `~/.gc/config.local.yml` (мёржится в конфиг). `config.yml` юзера не трогаем. |
| 3 | Объём команд | Полный цикл: install+wire, list, remove. |
| 4 | Семантика remove | **Полный снос**: un-wire + деинсталл из venv (по решению пользователя — «делаем по-взрослому»). |
| 5 | Плейсхолдеры в сниппетах | Резолвить при wire: шаблоны `{npx}`/`{cache}` из install-state/`gc_paths`. |
| 6 | install авто-wire'ит | Да: install (одного или всех) авто-подключает то, что успешно поставил. `gc-mcp-install` перестаёт быть install-only. |
| 7 | Поверхность команд | Зонтичный `gc-mcp <subcommand>`; `gc-mcp-install` — тонкий алиас на `gc-mcp install` (back-compat). |
| 8 | Как рецепт объявляет деинсталл | **Single source of truth**: dist-name деривируется где есть надёжный источник (pip-типы), объявляется явным полем только где вывод ненадёжен (npx → `npm_package`). Без эвристик-парсинга, без дублирования. |

---

## 4. Поверхность команд

```
gc-mcp install [name]            # name → один + wire; без name → все + wire
gc-mcp list                      # доступно / installed (venv) / wired (active)
gc-mcp remove <name>             # un-wire + деинсталл из venv (полный снос)
gc-mcp remove --keep-package X   # только un-wire (пакет в venv остаётся; быстрый re-install)
gc-mcp-install [...]             # тонкий алиас → gc-mcp install [...] (back-compat)
```

---

## 5. Архитектура и компоненты

```
gc-mcp install office
   ├─(1) resolve_agent_manifest_path → найти запись "office" по имени
   ├─(2) install_entry(office)              [переиспускаем существующее, per-recipe]
   └─(3) wire(office): сниппет → резолв {npx}/{cache} → upsert mcp.servers.office
                       в ~/.gc/config.local.yml (managed)

Агент при загрузке: config.yml(base) ⊕ config.local.yml(managed) ⊕ <cwd>/.gc(project)
```

| Файл | Слой | Ответственность |
|---|---|---|
| `src/mcp_runtime/cli.py` (**new**) | контроллер | `gc-mcp` entry: парсинг сабкоманд (install/list/remove), оркестрация install→wire / list / remove |
| `src/mcp_runtime/wiring.py` (**new**) | контроллер | сниппет → резолв плейсхолдеров → upsert/remove `mcp.servers.<name>` в managed-файле; чтение managed-файла для list/remove |
| `src/mcp_runtime/paths.py` | утилита | путь managed-файла `~/.gc/config.local.yml` (через `gc_paths()["config"]`) |
| `recommended_mcp/install.py` | контроллер | + хелпер «найти запись по имени»; + `uninstall_entry(entry)` (per-type деинсталл, зеркало `install_entry`) + резолвер dist-name |
| `recommended_mcp/manifest.py` | домен | + опциональное поле `InstallSpec.npm_package` (только для npx) |
| `src/config.py::load_config` | контроллер | + merge managed-файла (между base и project-local) |
| `src/mcp_runtime/install_cli.py` | контроллер | становится алиасом → `cli.main(["install", ...])` |

Managed-файл — **plain YAML** (`yaml.safe_dump`); машинно-владеемый, комментов нет, дрейфа комментов нет.

---

## 6. Поток данных

**install `<name>`:**
1. resolve манифест → найти запись по имени (нет → ошибка + список доступных).
2. `install_entry` (pip/npx в venv). Если упал / скипнут платформой → **не** wire'им.
3. `wire(name)`: читаем `recommended_mcp/<name>/agent_config.snippet.yml` → подставляем `{npx}`/`{cache}` → deep-upsert `mcp.servers.<name>` в `config.local.yml` (idempotent — повтор перезаписывает запись).

**install (без имени):** как сейчас (все рецепты, изоляция падений из Task 7), + wire каждого **успешно установленного**.

> Наследие packaging-фикса (актуально): source-рецепты `office/science/computer` работают **в обоих методах**. `external/` в wheel не едет, поэтому в **method-2** (без клона) `_install_source` при отсутствии локальных исходников ставит их из git-подкаталога репо: `pip install "git+<repo_url>#subdirectory=external/<name>"` (`repo_url` — в манифесте `recommended_mcp/config.yml`; публичный анонимный https, в т.ч. через корп-прокси). В **method-1** (клон) — из локальных исходников. wire происходит в обоих случаях. Остаётся v2: произвольные источники (`pypi:`/`git:`/`local:`) **вне** курируемого манифеста + публикация во внутренний индекс.

**remove `<name>`:**
1. un-wire: удалить `mcp.servers.<name>` из `config.local.yml` (нет записи → no-op + инфо).
2. (если не `--keep-package`) `uninstall_entry(entry)` — per-type:
   ```
   pypi   → dist из package-спеки            → pip uninstall -y <dist>
   source → dist из <source>/pyproject name   → pip uninstall -y <dist>
   wheel  → dist из метаданных wheel          → pip uninstall -y <dist>
   npx    → entry.install.npm_package          → npm uninstall -g <pkg> (через cached node)
   docker → вне scope (в дефолт-манифесте нет) → сообщение
   ```

**list:** managed-файл (`mcp.servers` = wired) + `query_installed_version` по venv (installed?) → таблица `name · transport · installed · wired`.

---

## 7. Плейсхолдеры (резолв при wire)

Фикс-набор шаблонов в сниппетах:
- `{npx}` → путь cached-npx (`_node_install_paths(agent_dep_dir)["npx"]`);
- `{cache}` → `gc_paths()["cache"]`.

Меняем сниппет `recommended_mcp/browser_playwright/agent_config.snippet.yml` с `<путь npx из install.py>` на `{npx}`/`{cache}`. Verbatim-сниппеты (office/science/computer/ddgs) — без шаблонов, проходят как есть. Неизвестный `{...}` → оставить + warn.

---

## 8. Config-merge (точечно)

`load_config`, после base, перед project-local:
```python
managed = gc_paths()["config"] / "config.local.yml"
if managed.is_file():
    raw = deep_merge(raw, yaml.safe_load(managed) or {})
```
`mcp.servers` — dict, мёржится по ключам: ручные сервера в `config.yml` + managed-сервера сосуществуют. Мёржим **всегда**, когда файл есть (глобальное состояние «что юзер поставил»; доступно независимо от `--config`).

---

## 9. Single source of truth для деинсталла (принцип §3.8)

`uninstall_entry` — per-type диспетч; каждый тип сам добывает цель сноса:
- **pip-типы** (pypi/source/wheel): dist-name **деривируется** (pypi → из `package`; source → `[project].name` из `<source>/pyproject.toml`; wheel → метаданные). Не дублируем — иначе два источника истины разъедутся.
- **npx**: имя npm-пакета надёжно нигде не лежит → **объявляется** явным `InstallSpec.npm_package` (одно поле, заполняется только в playwright-рецепте). Без хрупкого парсинга install-команд.

Гарантии: `pip uninstall <dist>` сносит **только сам пакет MCP**, зависимости не трогает (другие MCP в общем venv целы). npx — best-effort (`npm uninstall -g`), кэш браузеров остаётся (v2).

---

## 10. Обработка ошибок

- `<name>` не в манифесте → ясная ошибка + список доступных имён.
- install упал → не wire'им (wire только на успех); сводка как в install-изоляции.
- managed-файл отсутствует → создаём при первом wire.
- remove несуществующего wiring → no-op + сообщение; деинсталл не-установленного → no-op + инфо.
- dist-name не резолвится / pip uninstall упал → un-wire уже сделан (главное достигнуто), деинсталл → warn, не валим.
- идемпотентность: повторный install/wire перезаписывает запись (без дублей).

---

## 11. Тесты

- `wiring`: сниппет (+`{npx}`/`{cache}`) → резолв; upsert (create+update); remove; неизвестный плейсхолдер → warn. Hermetic (monkeypatch `gc_paths`, фейковый сниппет/install-state).
- `config-merge`: `config.local.yml` `mcp.servers` мёржится в загруженный конфиг (deep_merge, аккумулирует с base-серверами).
- `install-by-name`: выбор верного рецепта из манифеста; неизвестное имя → ошибка.
- `uninstall_entry`: pypi (pip uninstall с верным dist), source (dist прочитан из pyproject), npx (npm uninstall -g `npm_package`), не-установлен → no-op. Monkeypatch `subprocess`.
- `remove` e2e: un-wire + uninstall оба вызваны; `--keep-package` → только un-wire.
- `list`: отражает managed (wired) + installed (venv).
- CLI-smoke: install/list/remove диспетчатся; `gc-mcp-install` = `gc-mcp install` (all).

---

## 12. Вне scope (v2)

Произвольные источники (`pypi:`/`git:`/`local:`); сбор orphan-зависимостей; GC кэша браузеров playwright; каталог/реестр; публикация MCP во внутренний индекс.
