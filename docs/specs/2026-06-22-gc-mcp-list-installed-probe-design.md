# `gc-mcp list` — честная колонка INSTALLED — Design

**Дата:** 2026-06-22
**Статус:** согласовано, готово к плану
**Ветка:** feat/agent-instructions-and-gc-guard

## Проблема

`gc-mcp list` показывает `INSTALLED = —` у всех MCP кроме `duckduckgo_search`, хотя все реально установлены и подключены (`WIRED = yes`).

**Корень (подтверждён):** колонка вычисляется в [_cmd_list](../../src/mcp_runtime/cli.py) так:
```python
dist = _dist_name(e, root) if e.install.type in ("pypi", "source", "wheel") else None
installed = "yes" if (dist and query_installed_version(dist)) else "—"
```
- Для `source` `_dist_name` ([recommended_mcp/install.py](../../recommended_mcp/install.py)) читает имя дистрибутива из `recipe_root/../external/<name>/pyproject.toml` **на момент list**.
- В wheel ([pyproject.toml:41-50](../../pyproject.toml)) бандлится только `external/ddgs-mcp`; `computer-mcp`/`office-mcp`/`science-mcp` ставятся method-2 (git-подкаталог), исходники локально **не остаются**.
- Итог: `_dist_name` для `ddgs` резолвится (исходники забандлены), для остальных source → `None` → `—`, **хотя пакеты реально стоят в общем gc-MCP-venv** (`gc-mcp install` ставит их туда pip-ом; потому и WIRED).
- `npx` (browser_playwright) как python-dist не пробится вообще → `—`.

То есть `—` ложно читается как «не установлен», а на деле = «пробник не смог определить».

## Решение (Approach A + npx-проба)

Сделать пробник установки **не зависящим от наличия source-папки на диске** и проверяющим каждый тип в правильном месте. Имя python-дистрибутива объявляется в манифесте декларативно (как `package` у pypi). Колонка становится трёхзначной: `yes` / `—` / `?`.

### Компонент 1 — схема манифеста

`recommended_mcp/config.yml`: для source-записей добавить явное `install.dist` (имя python-дистрибутива):
```yaml
computer            install.dist: computer-mcp
office              install.dist: office-mcp
science             install.dist: science-mcp
duckduckgo_search   install.dist: ddgs-mcp
```
Канонический файл манифеста один (demo/default-варианта у MCP-манифеста нет — синхронизировать нечего).

### Компонент 2 — парсинг (`recommended_mcp/manifest.py`)

`InstallSpec` += поле `dist: str | None = None`. `_parse_entry` читает `install_raw.get("dist")` → строка или None (по образцу `package`/`npm_package`).

### Компонент 3 — `_dist_name` (`recommended_mcp/install.py`)

Приоритет источника имени:
1. `entry.install.dist`, если задано (source-dir-независимо);
2. иначе текущий фолбэк: `pypi` → из `package`; `source` → из source-pyproject (dev/бандл); `wheel` → из имени `.whl`.

**Бонус:** это чинит и `gc-mcp remove` для method-2 source-MCP — сейчас `uninstall_entry` зовёт `_dist_name`, без source-папки получает `None` и пропускает деинсталл с warning'ом. С `install.dist` имя резолвится.

### Компонент 4 — npx-проба (`recommended_mcp/install.py`)

Новый best-effort хелпер:
```python
def npm_global_has(npm_package: str, agent_dep_dir: Path) -> bool | None:
    """True/False есть ли пакет в node-окружении агента; None — не смогли проверить
    (npm недоступен / ошибка)."""
```
Реализация: `npm = _node_install_paths(agent_dep_dir)["npm"]`; если `npm` не существует → `None`. Иначе `subprocess.run([str(npm), "ls", "-g", npm_package, "--depth=0"], capture_output=True, check=False)` → `True` если `returncode == 0`, иначе `False`. Исключения (OSError) → `None`.

### Компонент 5 — `_cmd_list` (`src/mcp_runtime/cli.py`) — трёхзначный статус

Вынести вычисление в хелпер `_installed_status(entry, root, agent_dep_dir) -> str`:
- `type in ("pypi", "source", "wheel")`:
  - `name = _dist_name(entry, root)`; если `name is None` → `"?"`;
  - иначе `"yes" if query_installed_version(name) else "—"`.
- `type == "npx"`:
  - `npm_package` пуст → `"?"`;
  - иначе `res = npm_global_has(npm_package, agent_dep_dir)`; `None → "?"`, `True → "yes"`, `False → "—"`.
- иначе (docker и прочее) → `"?"`.

Семантика колонки: **`yes`** = установлен; **`—`** = проверили, не установлен; **`?`** = проверить не удалось. `_cmd_list` получает `agent_dep_dir` через существующий `_agent_dep_dir()`.

## Тестирование (pytest)

1. **Парсинг:** запись с `install.dist: foo-mcp` → `InstallSpec.dist == "foo-mcp"`; без поля → `None`.
2. **`_dist_name` приоритет:** entry с `install.dist` set и **без** source-папки на диске → возвращает `install.dist` (не лезет в pyproject); entry без `dist`, но с существующей source-pyproject → фолбэк на `[project].name`.
3. **`_installed_status`:**
   - source с `dist`, `query_installed_version` → версия (мок) → `"yes"`;
   - source с `dist`, `query_installed_version` → None → `"—"`;
   - source без `dist` и без source-папки (мок `_dist_name`→None) → `"?"`;
   - npx, `npm_global_has`→True/False/None → `"yes"/"—"/"?"`.
4. **Enforcement-guard:** **каждая** source-запись бандл-манифеста (`recommended_mcp/config.yml`) обязана иметь непустой `install.dist` — иначе тест падает (требование не на честном слове).
5. **Дрейф-guard:** для записей, чья source-папка существует на диске, `install.dist == [project].name` из её `pyproject.toml` (ловит расхождение объявленного и реального имени).

Регрессии: прогнать `tests/recommended_mcp/` (манифест, install, uninstall, runtime).

## Слои и ответственность

- Имя дистрибутива (`install.dist`, `_dist_name`) и npx-проба — слой `recommended_mcp/install.py` + схема `manifest.py` (технический слой установки, владелец имени пакета).
- Композиция статуса для отображения (`_installed_status`) — `src/mcp_runtime/cli.py` (CLI-контроллер `gc-mcp`).
- Новых зависимостей и состояния не вводим; манифест остаётся единственным декларативным источником описания MCP.

## Что вне scope

- Вариант B (запись имени при установке через diff venv) — отклонён: лишнее состояние + эвристика на method-2.
- Авто-вывод имени из basename source-папки — отклонён: конвенция, не гарантия (будущий MCP с `external/foo` и `[project].name = foo-server` её ломает).
- Изменение самой установки/wiring — не трогаем; только пробник чтения статуса.
