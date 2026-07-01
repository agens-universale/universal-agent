# `gc-mcp list` — честная колонка INSTALLED — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать колонку INSTALLED в `gc-mcp list` честной по всем install-типам и не зависящей от наличия source-папки на диске.

**Architecture:** Имя python-дистрибутива объявляется в манифесте (`install.dist`, как `package` у pypi); `_dist_name` отдаёт его приоритетно (source-dir-независимо); добавляется npx-проба `npm ls -g`; `_cmd_list` выводит трёхзначный статус `yes`/`—`/`?`.

**Tech Stack:** Python ≥3.10 (tomllib-путь требует 3.11, как в существующем `_dist_name`), pytest. Абсолютные импорты от `src/`; `recommended_mcp` — top-level пакет.

## Global Constraints

- Абсолютные импорты (`from mcp_runtime...`, `from recommended_mcp...`), не относительные.
- Тесты на pytest; прогон `pytest tests/recommended_mcp/...`.
- Значения статуса колонки — РОВНО: `yes`, `—` (em-dash U+2014, тот же символ, что уже в коде), `?`. Семантика: `yes`=установлен; `—`=проверили, не установлен; `?`=проверить не удалось.
- Каждая `source`-запись бандл-манифеста `recommended_mcp/config.yml` ОБЯЗАНА иметь непустой `install.dist` (enforcement-тест).
- Новых зависимостей и персистентного состояния не вводить; манифест — единственный декларативный источник.
- Манифест один канонический файл (`recommended_mcp/config.yml`); demo/default-варианта у него нет.

## File Structure

- `recommended_mcp/manifest.py` — `InstallSpec` += `dist`; `_parse_entry` читает его.
- `recommended_mcp/install.py` — `_dist_name` приоритет `install.dist`; новый `npm_global_has`.
- `src/mcp_runtime/cli.py` — `_installed_status` + правка `_cmd_list`.
- `recommended_mcp/config.yml` — `install.dist` у 4 source-записей.
- Тесты: `tests/recommended_mcp/test_manifest.py` (парсинг + guards), `test_mcp_uninstall.py` (`_dist_name`), `test_install.py` (`npm_global_has`), новый `test_mcp_list.py` (`_installed_status`).

---

## Task 1: Схема `install.dist` + парсинг

**Files:**
- Modify: `recommended_mcp/manifest.py` (`InstallSpec`, `_parse_entry`)
- Test: `tests/recommended_mcp/test_manifest.py`

**Interfaces:**
- Produces: `InstallSpec.dist: str | None` (None по умолчанию).

- [ ] **Step 1: Написать падающие тесты**

В [tests/recommended_mcp/test_manifest.py](../../tests/recommended_mcp/test_manifest.py) добавить (хелпер `_write` уже есть в файле):

```python
def test_source_mcp_with_dist(tmp_path):
    f = _write(tmp_path, """
        mcps:
          - name: office
            path: ./office
            install:
              type: source
              source: ../external/office-mcp
              dist: office-mcp
    """)
    assert load_manifest(f)[0].install.dist == "office-mcp"


def test_install_dist_defaults_none(tmp_path):
    f = _write(tmp_path, """
        mcps:
          - name: science
            path: ./science
            install: {type: source, source: ../external/science-mcp}
    """)
    assert load_manifest(f)[0].install.dist is None
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/recommended_mcp/test_manifest.py::test_source_mcp_with_dist -v`
Expected: FAIL — `TypeError: ... unexpected keyword 'dist'` или `AttributeError: ... 'dist'`.

- [ ] **Step 3: Добавить поле в `InstallSpec`**

В [recommended_mcp/manifest.py](../../recommended_mcp/manifest.py) в dataclass `InstallSpec` после `npm_package`:

```python
    # Для type=pypi: pip-спецификация, например ddgs-mcp==0.1.0
    package: str | None = None
    # Для type=npx: имя npm-пакета для `npm uninstall -g` (деинсталл). Деривировать
    # из install-команд ненадёжно (scoped-имена + версия), поэтому объявляется явно.
    npm_package: str | None = None
    # Имя python-дистрибутива (для проверки установки/деинсталла), source-dir-независимо.
    # Для pypi имя берётся из package; для source — объявляется здесь явно.
    dist: str | None = None
```

- [ ] **Step 4: Прочитать `dist` в `_parse_entry`**

В `_parse_entry`, рядом с разбором `npm_package` (после строки `install_npm_package = ...`):

```python
    dist_raw = install_raw.get("dist")
    install_dist = str(dist_raw).strip() if dist_raw else None
```

И в конструктор `InstallSpec(...)` добавить аргумент:

```python
    install = InstallSpec(
        type=install_raw.get("type", ""),
        platforms=tuple(install_raw.get("platforms", ()) or ()),
        commands=commands,
        source=install_source,
        package=install_package,
        npm_package=install_npm_package,
        dist=install_dist,
    )
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `pytest tests/recommended_mcp/test_manifest.py -v`
Expected: PASS (новые 2 + существующие).

- [ ] **Step 6: Commit**

```bash
git add recommended_mcp/manifest.py tests/recommended_mcp/test_manifest.py
git commit -m "mcp(manifest): install.dist — явное имя python-дистрибутива source-MCP"
```

---

## Task 2: `_dist_name` приоритет `install.dist`

**Files:**
- Modify: `recommended_mcp/install.py` (`_dist_name`)
- Test: `tests/recommended_mcp/test_mcp_uninstall.py`

**Interfaces:**
- Consumes: `InstallSpec.dist` (Task 1).
- Produces: `_dist_name(entry, root)` возвращает `install.dist` приоритетно.

- [ ] **Step 1: Написать падающие тесты**

В [tests/recommended_mcp/test_mcp_uninstall.py](../../tests/recommended_mcp/test_mcp_uninstall.py) добавить (импорты `MCPEntry, InstallSpec`, `install as inst`, `Path` уже есть):

```python
def test_dist_name_prefers_explicit_dist(tmp_path):
    # source-папки на диске НЕТ — _dist_name всё равно отдаёт install.dist
    e = MCPEntry(name="office", path=Path("."),
                 install=InstallSpec(type="source", source=Path("../external/office-mcp"),
                                     dist="office-mcp"))
    assert inst._dist_name(e, root=tmp_path) == "office-mcp"


def test_dist_name_fallback_pyproject_when_no_dist(tmp_path):
    src = tmp_path / "science-mcp"
    src.mkdir()
    (src / "pyproject.toml").write_text(
        '[project]\nname = "science-mcp"\nversion = "1"\n', encoding="utf-8")
    e = MCPEntry(name="science", path=Path("."),
                 install=InstallSpec(type="source", source=Path("science-mcp")))
    assert inst._dist_name(e, root=tmp_path) == "science-mcp"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/recommended_mcp/test_mcp_uninstall.py::test_dist_name_prefers_explicit_dist -v`
Expected: FAIL — без source-папки текущий `_dist_name` вернёт `None`, а ждём `"office-mcp"`.

- [ ] **Step 3: Добавить приоритет `install.dist`**

В [recommended_mcp/install.py](../../recommended_mcp/install.py) в начале `_dist_name` (после docstring, перед `itype = entry.install.type`):

```python
def _dist_name(entry: MCPEntry, root: Path = Path(".")) -> str | None:
    """Имя дистрибутива для `pip uninstall` / проверки установки.
    Приоритет: явное install.dist (source-dir-независимо) → тип-специфичный фолбэк
    (pypi → package; source → [project].name из pyproject; wheel → имя .whl)."""
    if entry.install.dist:
        return entry.install.dist
    itype = entry.install.type
```

(Остальное тело без изменений.)

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/recommended_mcp/test_mcp_uninstall.py -v`
Expected: PASS (новые 2 + существующие `test_dist_name_*`, `test_uninstall_*` — фолбэк для записей без `dist` сохранён).

- [ ] **Step 5: Commit**

```bash
git add recommended_mcp/install.py tests/recommended_mcp/test_mcp_uninstall.py
git commit -m "mcp(install): _dist_name приоритетно отдаёт install.dist (+ чинит remove method-2)"
```

---

## Task 3: npx-проба `npm_global_has`

**Files:**
- Modify: `recommended_mcp/install.py` (новая функция `npm_global_has`)
- Test: `tests/recommended_mcp/test_install.py`

**Interfaces:**
- Produces: `npm_global_has(npm_package: str, agent_dep_dir: Path) -> bool | None` (True/False/None).

- [ ] **Step 1: Написать падающие тесты**

В [tests/recommended_mcp/test_install.py](../../tests/recommended_mcp/test_install.py) добавить (добавь `import types` и `from pathlib import Path` если их нет; `from recommended_mcp import install as inst` — проверь импорт в шапке, используй имеющийся алиас):

```python
def test_npm_global_has_missing_npm(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "_node_install_paths",
                        lambda dep: {"npm": tmp_path / "nope"})
    assert inst.npm_global_has("@playwright/mcp", tmp_path) is None


def test_npm_global_has_present(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    npm.write_text("", encoding="utf-8")
    monkeypatch.setattr(inst, "_node_install_paths", lambda dep: {"npm": npm})
    monkeypatch.setattr(inst.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert inst.npm_global_has("@playwright/mcp", tmp_path) is True


def test_npm_global_has_absent(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    npm.write_text("", encoding="utf-8")
    monkeypatch.setattr(inst, "_node_install_paths", lambda dep: {"npm": npm})
    monkeypatch.setattr(inst.subprocess, "run",
                        lambda argv, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert inst.npm_global_has("@playwright/mcp", tmp_path) is False
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/recommended_mcp/test_install.py::test_npm_global_has_present -v`
Expected: FAIL — `AttributeError: module 'recommended_mcp.install' has no attribute 'npm_global_has'`.

- [ ] **Step 3: Реализовать `npm_global_has`**

В [recommended_mcp/install.py](../../recommended_mcp/install.py) рядом с `_node_install_paths` добавить:

```python
def npm_global_has(npm_package: str, agent_dep_dir: Path) -> bool | None:
    """Есть ли npm-пакет в node-окружении агента (`npm ls -g`).
    True/False — проверили; None — проверить не удалось (npm недоступен / ошибка)."""
    npm = _node_install_paths(agent_dep_dir)["npm"]
    if not Path(npm).exists():
        return None
    try:
        result = subprocess.run(
            [str(npm), "ls", "-g", npm_package, "--depth=0"],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    return result.returncode == 0
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/recommended_mcp/test_install.py -v`
Expected: PASS (новые 3 + существующие).

- [ ] **Step 5: Commit**

```bash
git add recommended_mcp/install.py tests/recommended_mcp/test_install.py
git commit -m "mcp(install): npm_global_has — best-effort проба npx-пакета в node-окружении"
```

---

## Task 4: `_installed_status` + трёхзначный `_cmd_list`

**Files:**
- Modify: `src/mcp_runtime/cli.py` (`_installed_status`, `_cmd_list`)
- Test: `tests/recommended_mcp/test_mcp_list.py` (создать)

**Interfaces:**
- Consumes: `_dist_name` (Task 2), `npm_global_has` (Task 3), `query_installed_version`.
- Produces: `_installed_status(entry, root, agent_dep_dir) -> str` (`"yes"`/`"—"`/`"?"`).

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/recommended_mcp/test_mcp_list.py`:

```python
"""_installed_status: трёхзначный статус INSTALLED для gc-mcp list."""
from pathlib import Path

from mcp_runtime import cli
from recommended_mcp.manifest import MCPEntry, InstallSpec


def _src(dist=None):
    return MCPEntry(name="office", path=Path("."),
                    install=InstallSpec(type="source", source=Path("../external/office-mcp"), dist=dist))


def test_status_source_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: "office-mcp")
    monkeypatch.setattr("mcp_runtime.venv.query_installed_version", lambda n: "1.0")
    assert cli._installed_status(_src("office-mcp"), Path("."), Path(".")) == "yes"


def test_status_source_not_installed(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: "office-mcp")
    monkeypatch.setattr("mcp_runtime.venv.query_installed_version", lambda n: None)
    assert cli._installed_status(_src("office-mcp"), Path("."), Path(".")) == "—"


def test_status_source_unknown_name(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install._dist_name", lambda e, r: None)
    assert cli._installed_status(_src(), Path("."), Path(".")) == "?"


def test_status_npx_present(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install.npm_global_has", lambda pkg, dep: True)
    e = MCPEntry(name="pw", path=Path("."),
                 install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    assert cli._installed_status(e, Path("."), Path(".")) == "yes"


def test_status_npx_undetermined(monkeypatch):
    monkeypatch.setattr("recommended_mcp.install.npm_global_has", lambda pkg, dep: None)
    e = MCPEntry(name="pw", path=Path("."),
                 install=InstallSpec(type="npx", npm_package="@playwright/mcp"))
    assert cli._installed_status(e, Path("."), Path(".")) == "?"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/recommended_mcp/test_mcp_list.py::test_status_source_installed -v`
Expected: FAIL — `AttributeError: module 'mcp_runtime.cli' has no attribute '_installed_status'`.

- [ ] **Step 3: Добавить `_installed_status` и переписать `_cmd_list`**

В [src/mcp_runtime/cli.py](../../src/mcp_runtime/cli.py) добавить функцию (над `_cmd_list`):

```python
def _installed_status(entry, root: Path, agent_dep_dir: Path) -> str:
    """yes = установлен; — = проверили, не установлен; ? = проверить не удалось."""
    from recommended_mcp.install import _dist_name, npm_global_has
    from mcp_runtime.venv import query_installed_version
    itype = entry.install.type
    if itype in ("pypi", "source", "wheel"):
        name = _dist_name(entry, root)
        if name is None:
            return "?"
        return "yes" if query_installed_version(name) else "—"
    if itype == "npx":
        pkg = entry.install.npm_package
        if not pkg:
            return "?"
        res = npm_global_has(pkg, agent_dep_dir)
        return "?" if res is None else ("yes" if res else "—")
    return "?"
```

Переписать `_cmd_list` (заменить тело целиком):

```python
def _cmd_list() -> int:
    entries, root = _load_manifest_entries()
    from mcp_runtime import wiring
    dep = _agent_dep_dir()
    wired = wiring.wired_servers()
    print(f"{'NAME':22} {'TRANSPORT':14} {'INSTALLED':10} WIRED")
    for e in entries:
        installed = _installed_status(e, root, dep)
        srv = wired.get(e.name) or {}
        transport = srv.get("transport", "—")
        print(f"{e.name:22} {transport:14} {installed:10} "
              f"{'yes' if e.name in wired else '—'}")
    return 0
```

(Удаляются прежние локальные импорты `query_installed_version`/`_dist_name` из `_cmd_list` — теперь они внутри `_installed_status`.)

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/recommended_mcp/test_mcp_list.py -v`
Expected: PASS (5/5).

- [ ] **Step 5: Commit**

```bash
git add src/mcp_runtime/cli.py tests/recommended_mcp/test_mcp_list.py
git commit -m "mcp(cli): gc-mcp list — трёхзначный INSTALLED (yes/—/?) по всем типам"
```

---

## Task 5: `install.dist` в манифесте + guard-тесты

**Files:**
- Modify: `recommended_mcp/config.yml`
- Test: `tests/recommended_mcp/test_manifest.py`

**Interfaces:**
- Consumes: парсинг `install.dist` (Task 1), `_dist_name` фолбэк (Task 2).

- [ ] **Step 1: Написать падающие guard-тесты**

В [tests/recommended_mcp/test_manifest.py](../../tests/recommended_mcp/test_manifest.py) добавить:

```python
def test_every_source_entry_declares_dist():
    """Каждая source-запись бандл-манифеста обязана нести install.dist."""
    from recommended_mcp.manifest import default_manifest_path, load_manifest
    from mcp_runtime.paths import recipe_root
    entries = load_manifest(default_manifest_path(recipe_root()))
    for e in entries:
        if e.install.type == "source":
            assert e.install.dist, f"{e.name}: source-запись обязана иметь install.dist"


def test_manifest_dist_matches_pyproject_when_source_present():
    """Где source-папка на диске есть — install.dist == [project].name из pyproject."""
    import dataclasses
    from recommended_mcp import install as inst
    from recommended_mcp.manifest import default_manifest_path, load_manifest
    from mcp_runtime.paths import recipe_root
    root = recipe_root()
    for e in load_manifest(default_manifest_path(root)):
        if e.install.type == "source" and e.install.dist and e.install.source:
            pp = (root / e.install.source).resolve() / "pyproject.toml"
            if not pp.is_file():
                continue
            no_dist = dataclasses.replace(
                e, install=dataclasses.replace(e.install, dist=None))
            assert e.install.dist == inst._dist_name(no_dist, root), \
                f"{e.name}: install.dist={e.install.dist} != имя из pyproject"
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/recommended_mcp/test_manifest.py::test_every_source_entry_declares_dist -v`
Expected: FAIL — у source-записей манифеста ещё нет `install.dist` → `assert e.install.dist` падает.

- [ ] **Step 3: Добавить `install.dist` в манифест**

В [recommended_mcp/config.yml](../../recommended_mcp/config.yml) в `install`-блок каждой source-записи добавить `dist`:

```yaml
  - name: computer
    path: ./computer
    install:
      type: source
      source: ../external/computer-mcp
      dist: computer-mcp
      platforms: [win32]

  - name: office
    path: ./office
    install:
      type: source
      source: ../external/office-mcp
      dist: office-mcp

  - name: science
    path: ./science
    install:
      type: source
      source: ../external/science-mcp
      dist: science-mcp
    run:
      type: command
      command: science-mcp
      args: ["--transport", "http"]

  - name: duckduckgo_search
    path: ./duckduckgo_search
    install:
      type: source
      source: ../external/ddgs-mcp
      dist: ddgs-mcp
```

(Блок `browser_playwright` (`type: npx`) не трогаем — у него `npm_package`, не `dist`.)

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/recommended_mcp/test_manifest.py -v`
Expected: PASS (оба guard'а + существующие). `test_manifest_dist_matches_pyproject_when_source_present` проверит совпадение имён там, где `external/<name>` на диске.

- [ ] **Step 5: Commit**

```bash
git add recommended_mcp/config.yml tests/recommended_mcp/test_manifest.py
git commit -m "mcp(manifest): объявить install.dist у source-MCP + enforcement/дрейф guard"
```

---

## Финальная проверка

- [ ] Прогнать весь набор recommended_mcp: `pytest tests/recommended_mcp/ -q` — без новых падений.
- [ ] (Опц., ручное) На машине с установленными MCP: `gc-mcp list` → computer/office/science = `yes`, browser_playwright = `yes` (npm-проба), duckduckgo_search = `yes`.

---

## Self-Review

**Spec coverage:**
- Компонент 1 (схема `install.dist`) → Task 1. ✓
- Компонент 2 (парсинг) → Task 1. ✓
- Компонент 3 (`_dist_name` приоритет + бонус remove) → Task 2. ✓
- Компонент 4 (npx-проба `npm_global_has`) → Task 3. ✓
- Компонент 5 (`_installed_status` + трёхзначный `_cmd_list`) → Task 4. ✓
- Манифест-значения + enforcement-guard + дрейф-guard → Task 5. ✓
- Тесты из спеки (1 парсинг, 2 приоритет _dist_name, 3 статусы, 4 enforcement, 5 дрейф) → покрыты во всех 5 задачах. ✓

**Placeholder scan:** плейсхолдеров нет — каждый шаг несёт готовый код и точную команду.

**Type consistency:** `InstallSpec.dist: str | None` (Task 1) ← читается в `_dist_name` (Task 2), `_installed_status` (Task 4), guards (Task 5). `npm_global_has(npm_package: str, agent_dep_dir: Path) -> bool | None` (Task 3) ← вызывается в `_installed_status` (Task 4) с теми же типами. `_installed_status(entry, root, agent_dep_dir) -> str` ← `_cmd_list` зовёт с `root` из манифеста и `dep=_agent_dep_dir()`. Имена/сигнатуры согласованы.
