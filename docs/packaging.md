[Назад к README](../readme.md)

# Как устроена упаковка: пять дистрибутивов, четыре пакета

Код разбит на настоящие пакеты с собственным пространством имён:
`gc_core` (SDK), `gc_cli`, `gc_tui`, `gc_server`. Каждый живёт в своём каталоге
под `packages/` и собирается своим манифестом.

## Профили

| Дистрибутив | Пакет | Модулей | Команды |
|---|---|---|---|
| `gc-core` | `gc_core` | 277 | **нет** — это библиотека |
| `gc-cli` | `gc_cli` | 3 | все пять |
| `gc-tui` | `gc_tui` | 112 | все пять |
| `gc-server` | `gc_server` | 12 | все пять |
| `gc-ua` | — (только лаунчер) | 1 | все пять |

`gc-cli`, `gc-tui` и `gc-server` зависят от `gc-core`; друг от друга — нет.
`gc-ua` тянет все четыре.

Ассеты (`prompts/`, `skills/`, `config.default.yml`) принадлежат `gc-core` и
лежат в `packages/core/assets/`. Фронтенды дотягиваются до них через
`gc_core.bundled_assets.bundled_root()` — он считает путь от своего `__file__`,
поэтому работает и из другого дистрибутива.

## Команды установки

```bash
uv tool install "gc-ua @ git+https://gitverse.ru/gigacode-ai/universal-agent"
uv tool install "gc-cli @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/cli"
uv tool install "gc-tui @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/tui"
uv tool install "gc-server @ git+https://gitverse.ru/gigacode-ai/universal-agent#subdirectory=packages/server"
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

## Манифесты

У каждого пакета свой каталог и свой манифест — путей к чужим файлам больше
нет:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/gc_tui"]
```

Так было не всегда: до разбиения на пакеты все модули лежали в общем `src/`, и
манифесты забирали из него срез через `force-include` с явным перечнем путей.
У ядра там было 48 записей — «весь `src/` минус лишнее» выразить нечем,
`force-include` умеет только включать. Переезд это убрал.

Ассеты ядра остались через `force-include`, потому что кладутся не внутрь
пакета, а рядом с ним:

```toml
[tool.hatch.build.targets.wheel.force-include]
"assets/prompts" = "prompts"
"assets/skills" = "skills"
"assets/config.default.yml" = "config.default.yml"
```

Проверить состав:

```bash
uv build --wheel -o dist packages/core
python -c "import zipfile;print(sorted(zipfile.ZipFile('dist/gc_core-0.5.0-py3-none-any.whl').namelist())[:20])"
```

## Главный инвариант: колёса не пересекаются

Все пять колёс обязаны иметь **непересекающиеся** множества путей. Если два
колеса заявят один и тот же файл, установка двух профилей затрёт его, и чья
версия победит — решит порядок распаковки.

Инвариант закреплён тестом `tests/test_packaging_wheel.py::test_wheels_are_pairwise_disjoint`.
После разбиения на пакеты инвариант держится сам собой: каждый пакет лежит в
своём каталоге, и пересечься им нечем. До переезда это было не так — все
собирали срезы одного дерева, и корневые обрубки `src/__init__.py` и
`src/.gitignore` заявлялись сразу тремя дистрибутивами.

## Команды: почему их объявляет каждый профиль

`uv tool install` кладёт в PATH скрипты **только запрошенного дистрибутива**.
Если бы команды объявляла одна мета, установка `gc-tui` дала бы окружение вообще
без команд — так и было в первом варианте, поймано установкой.

Поэтому все манифесты приложений объявляют одинаковый набор из пяти команд
(`gc-core` — не объявляет: это библиотека):

```toml
[project.scripts]
gc-cli = "gc_cli.cli:main"
gc-ua = "gc_core.tui_entry:main"
gc-serve = "gc_core.serve_entry:main"
gc-mcp = "gc_core.mcp_runtime.cli:main"
gc-mcp-install = "gc_core.mcp_runtime.install_cli:main"
```

Следствие: команда `gc-ua` есть **всегда**, даже в headless-профиле, где пакета
`tui` физически нет. Чтобы она не падала трейсбеком, точка входа ведёт не в сам
`gc_tui`, а в шим `gc_core/tui_entry.py`, живущий в **ядре**:

```python
try:
    import gc_tui as _tui
except ImportError as exc:
    if getattr(exc, "name", None) in {"gc_tui", "textual", "rich", "pygments"}:
        print(_MISSING_TUI_MSG, file=sys.stderr)
        return 1
    raise
```

Ловится и отсутствие пакета, и отсутствие его зависимостей; любой другой
`ImportError` пробрасывается с трейсбеком, чтобы настоящая поломка не выдавалась
за «не установлен профиль». То же самое у `gc_core/serve_entry.py` для `gc-serve`.

Сообщение — строго однострочное: `tests/tui/test_runner_no_textual.py` требует
`err.count("\n") <= 1`.

## Как агент понимает, какой профиль установлен

`gc_core/app_version.py` перебирает `PROFILE_DISTS` в порядке убывания полноты и
берёт первый найденный:

```python
PROFILE_DISTS = ("gc-ua", "gc-tui", "gc-server", "gc-cli", "gc-core")
```

Мета идёт первой: если стоит она, профиль полный, даже когда рядом лежат её
листья. От этого зависят вывод версии и обновление.

Подкаталог берётся из `direct_url.json` (PEP 610) — там `subdirectory` лежит
верхним ключом:

```json
{"url":"...","vcs_info":{"vcs":"git","commit_id":"2ca3397..."},"subdirectory":"packages/tui"}
```

У меты этого ключа нет — она в корне репозитория. Все дистрибутивы одной
установки несут **один и тот же** `commit_id`: uv резолвит соседей из того же
коммита, потому что `[tool.uv.sources]` объявляет их членами workspace.

## Обновление сохраняет профиль

`gc_core.updater.runner.build_install_target` собирает строку переустановки тем же
профилем, что стоит:

```
gc-ua      -> gc-ua @ git+URL@master
gc-cli     -> gc-cli @ git+URL@master#subdirectory=packages/cli
gc-tui     -> gc-tui @ git+URL@master#subdirectory=packages/tui
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

Версия во всех пяти манифестах обязана совпадать, пины друг на друга —
точные (`gc-core==0.5.0`). Иначе можно собрать установку из разных коммитов.
Закреплено тестом `test_versions_are_pinned_together`.

Релиз становится атомарной операцией над пятью файлами: бампать надо все.

## Разработка

Тестовый набор гоняется из репозитория, а не из установленных колёс:

```bash
uv sync
uv run pytest
```

Тестовая группа перечисляет сторонние пакеты фронтендов **напрямую**, а не через
профили. Это не стилистика: установка профиля в dev-режиме скопировала бы
`gc_tui/` и `gc_server/` в `site-packages`, который идёт в `sys.path` раньше
каталогов `packages/*/src` — и прогон начал бы молча исполнять устаревшую копию.

`conftest.py` в корне кладёт на `sys.path` все `packages/*/src` плюс сам корень
(там лежит `recommended_mcp` — отдельный импортируемый пакет). Пути к исходникам
внутри тестов берутся из `tests/paths.py`, а не собираются вручную: при
следующем переезде правится один файл, а не сорок.

## Что сломается, если добавить модуль

Ничего: модуль кладётся в нужный пакет (`packages/<dist>/src/gc_*/`) и приезжает
в колесо автоматически — манифест забирает пакет целиком.

Внимания требуют **строковые** ссылки на модули. Кодмод правит `import`, но
модуль можно назвать и строкой: `monkeypatch.setattr("gc_core.x")`,
`sys.modules["gc_core.tools"]`, `f"gc_core.tools.{sub}"`. Около трети таких мест
при устаревшем имени не падают, а молча становятся no-op — цикл выселения
пустеет, заглушки протекают между тестами, набор зелёный. Проверка:

```bash
uv run python tools_dev/check_module_literals.py
uv run pytest tests/test_packaging_wheel.py
```
