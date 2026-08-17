[Назад к README](../readme.md)

# Как устроена упаковка: четыре профиля из одного дерева

Агент поставляется четырьмя дистрибутивами, которые собираются из **одного и
того же** `src/`. Ни один файл при этом никуда не переезжал и ни один импорт не
менялся — вся раскладка живёт в четырёх манифестах.

## Профили

| Дистрибутив | Что кладёт на диск | Манифест |
|---|---|---|
| `gc-ua` | ничего своего, тянет три остальных | `pyproject.toml` (корень) |
| `gc-cli` | ядро: 295 `.py` + промпты, скиллы, `config.default.yml`, `recommended_mcp/` | `packaging/cli/pyproject.toml` |
| `gc-tui` | `tui/`, `diff_tui/` (112 `.py`) + `textual`, `rich`, `pygments` | `packaging/tui/pyproject.toml` |
| `gc-server` | `server.py`, `server_cleanup_threads.py`, `rest_api/`, `swarm/` (12 `.py`) + `fastapi`, `uvicorn`, `psutil` | `packaging/server/pyproject.toml` |

`gc-tui` и `gc-server` зависят от `gc-cli`, поэтому ставятся вместе с ядром.
`gc-ua` зависит от всех трёх.

## Команды установки

```bash
uv tool install "gc-ua @ git+https://gitverse.ru/gigacode-ai/universal-agent"
uv tool install "gc-cli @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packaging/cli"
uv tool install "gc-tui @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packaging/tui"
uv tool install "gc-server @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packaging/server"
```

Смена профиля — это переустановка, нужен `--force`.

### Почему у частичных профилей есть `#subdirectory=`, а у `gc-ua` нет

Потому что **uv читает из git-репозитория только корневой `pyproject.toml`**.
Он не ищет членов workspace по имени. Проверяется одной командой:

```
$ uv pip install "gc-tui @ git+file:///path/to/repo"
× Failed to download and build `gc-tui @ git+file:///path/to/repo`
╰─▶ Package metadata name `gc-ua` does not match given name `gc-tui`
```

uv собрал корень, увидел там имя `gc-ua` и отказался выдавать его за `gc-tui`.
Поэтому листовой профиль нужно указывать вместе с каталогом его манифеста.

Отсюда же следует, что **короткая команда достаётся ровно одному профилю** —
тому, чьё имя стоит в корне. Мы отдали её полному агенту.

### Почему не `gc-ua[tui]`

Extras умеют только **добавлять** зависимости, вычитать нечем. Раз корень
(`gc-ua`) уже тянет всё, любой `gc-ua[что-угодно]` даёт то же самое. Поэтому
профили сделаны отдельными дистрибутивами, а не extras. `gc-ua[all]` оставлен
как пустой синоним полной установки — он ничего не добавляет и нужен только для
читаемости в скриптах.

## Как манифест собирает чужие файлы

Ни в одном из каталогов `packaging/*` кода нет. Каждый манифест забирает срез
общего `src/`:

```toml
[tool.hatch.build.targets.wheel]
only-include = []

[tool.hatch.build.targets.wheel.force-include]
"../../src/tui" = "tui"
"../../src/diff_tui" = "diff_tui"
```

`force-include` — **единственная** директива hatchling, принимающая пути выше
корня проекта. `only-include` и `packages` на таких путях падают с
`ValueError: ... must be relative`. Отсюда две особенности:

- у ядра перечень явный, все 48 модульных записей плюс 5 ассетных — «весь `src/`
  минус лишнее» выразить нельзя, `force-include` умеет только включать;
- слева всегда путь относительно каталога манифеста, справа — путь внутри колеса.

Проверить состав можно так:

```bash
uv build --wheel -o dist packaging/cli
python -c "import zipfile;print(sorted(zipfile.ZipFile('dist/gc_cli-0.4.0-py3-none-any.whl').namelist())[:20])"
```

## Главный инвариант: колёса не пересекаются

Все четыре колеса обязаны иметь **непересекающиеся** множества путей. Если два
колеса заявят один и тот же файл, установка двух профилей затрёт его, и чья
версия победит — решит порядок распаковки.

Инвариант закреплён тестом `tests/test_packaging_wheel.py::test_wheels_are_pairwise_disjoint`.
Там же проверяется, что в колесе ядра нет `tui/`, `server.py` и корневых
обрубков `src/__init__.py` и `src/.gitignore` — последние два раньше заявлялись
всеми тремя дистрибутивами сразу.

Ядро режется через `exclude` в корне… точнее, **больше не режется**: с переездом
ядра в `packaging/cli` там просто явный перечень. Корневой манифест теперь ничего
из `src/` не собирает вовсе.

## Команды: почему их объявляет каждый профиль

`uv tool install` кладёт в PATH скрипты **только запрошенного дистрибутива**.
Если бы команды объявляла одна мета, установка `gc-tui` дала бы окружение вообще
без команд — так и было в первом варианте, поймано установкой.

Поэтому все четыре манифеста объявляют одинаковый набор из пяти команд:

```toml
[project.scripts]
gc-cli = "cli:main"
gc-ua = "tui_entry:main"
gc-serve = "serve_entry:main"
gc-mcp = "mcp_runtime.cli:main"
gc-mcp-install = "mcp_runtime.install_cli:main"
```

Следствие: команда `gc-ua` есть **всегда**, даже в headless-профиле, где пакета
`tui` физически нет. Чтобы она не падала трейсбеком, точка входа ведёт не в сам
`tui`, а в шим `src/tui_entry.py`, живущий в **ядре**:

```python
try:
    import tui as _tui
except ImportError as exc:
    if getattr(exc, "name", None) in {"tui", "textual", "rich", "pygments"}:
        print(_MISSING_TUI_MSG, file=sys.stderr)
        return 1
    raise
```

Ловится и отсутствие пакета, и отсутствие его зависимостей; любой другой
`ImportError` пробрасывается с трейсбеком, чтобы настоящая поломка не выдавалась
за «не установлен профиль». То же самое у `src/serve_entry.py` для `gc-serve`.

Сообщение — строго однострочное: `tests/tui/test_runner_no_textual.py` требует
`err.count("\n") <= 1`.

## Как агент понимает, какой профиль установлен

`src/app_version.py` перебирает `PROFILE_DISTS` в порядке убывания полноты и
берёт первый найденный:

```python
PROFILE_DISTS = ("gc-ua", "gc-tui", "gc-server", "gc-cli")
```

Мета идёт первой: если стоит она, профиль полный, даже когда рядом лежат её
листья. От этого зависят вывод версии и обновление.

Подкаталог берётся из `direct_url.json` (PEP 610) — там `subdirectory` лежит
верхним ключом:

```json
{"url":"...","vcs_info":{"vcs":"git","commit_id":"2ca3397..."},"subdirectory":"packaging/tui"}
```

У меты этого ключа нет — она в корне репозитория. Все дистрибутивы одной
установки несут **один и тот же** `commit_id`: uv резолвит соседей из того же
коммита, потому что `[tool.uv.sources]` объявляет их членами workspace.

## Обновление сохраняет профиль

`updater.runner.build_install_target` собирает строку переустановки тем же
профилем, что стоит:

```
gc-ua      -> gc-ua @ git+URL@master
gc-cli     -> gc-cli @ git+URL@master#subdirectory=packaging/cli
gc-tui     -> gc-tui @ git+URL@master#subdirectory=packaging/tui
```

Без этого `--update` на headless-установке молча раздул бы её до полной (голый
`git+URL` всегда резолвится в корень), а на `gc-tui` — потерял бы TUI.

## Приватный репозиторий

Соседние профили резолвятся **по тому же URL, который набрал пользователь** —
это следствие `[tool.uv.sources] ... { workspace = true }`. Поэтому токены в
URL и форки работают без дополнительной настройки. Если бы профили ссылались
друг на друга через PEP 508 direct reference, URL зашился бы в METADATA колеса и
сломался бы у всех, кроме автора.

Ничего из этого в публичные индексы не публикуется: установка только из git.

## Версии

Версия во всех четырёх манифестах обязана совпадать, пины друг на друга —
точные (`gc-cli==0.4.0`). Иначе можно собрать установку из разных коммитов.
Закреплено тестом `test_versions_are_pinned_together`.

Релиз становится атомарной операцией над четырьмя файлами: бампать надо все.

## Разработка

Тестовый набор гоняется из репозитория, а не из установленных колёс:

```bash
uv sync
uv run pytest
```

Тестовая группа перечисляет сторонние пакеты фронтендов **напрямую**, а не через
профили. Это не стилистика: установка профиля в dev-режиме скопировала бы `tui/`
и `server.py` в `site-packages`, который идёт в `sys.path` раньше пути на
`<repo>/src` — и прогон начал бы молча исполнять устаревшую копию.

## Что сломается, если добавить модуль

Новый модуль в `src/` попадает в ядро, только если его добавить в перечень
`force-include` в `packaging/cli/pyproject.toml`. Иначе он просто не приедет в
колесо — тесты из репозитория этого не заметят, потому что гоняются по `src/`.

Проверка одна: `uv run pytest tests/test_packaging_wheel.py`. Она собирает все
четыре колеса и сверяет состав.
