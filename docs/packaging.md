[Назад к README](../readme.md)

# Как устроена упаковка: пять дистрибутивов, четыре пакета

Код разбит на настоящие пакеты с собственным пространством имён:
`gc_core` (SDK), `gc_cli`, `gc_tui`, `gc_server`. Каждый живёт в своём каталоге
под `packages/` и собирается своим манифестом.

## Профили

| Дистрибутив | Пакет | Модулей | Команды |
|---|---|---|---|
| `gc-core` | `gc_core` | 277 | **нет** — это библиотека |
| `gc-cli` | `gc_cli` | 3 | `gc-cli` + mcp |
| `gc-tui` | `gc_tui` | 112 | `gc-ua` + mcp |
| `gc-server` | `gc_server` | 12 | `gc-serve` + mcp |
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

Ассеты ядра идут через `force-include`, потому что в репозитории лежат рядом с
`src/`, а в колесо кладутся внутрь пакета:

```toml
[tool.hatch.build.targets.wheel.force-include]
"assets/prompts" = "gc_core/assets/prompts"
"assets/skills" = "gc_core/assets/skills"
"assets/config.default.yml" = "gc_core/assets/config.default.yml"
```

Внутрь пакета они переехали 18.08.2026. До этого попадали **рядом** с ним, прямо
в `site-packages`, и дистрибутив занимал там четыре родовых имени верхнего
уровня: `prompts`, `skills`, `external`, `recommended_mcp`. В Python 3 любой
каталог на `sys.path` — namespace-пакет, поэтому у встраивающего приложения
`import prompts` молча резолвился в наши промпты — проверено в чистом venv.
Ровно та болезнь, ради лечения которой ядро и делали SDK: было 45 родовых имён,
после разбиения осталось четыре, теперь два.

`recommended_mcp` и `external` пока остаются наверху: первый — настоящий
импортируемый пакет (`mcp_daemon_autostart` тянет `recommended_mcp.autostart`),
второй обязан быть его соседом, потому что манифест MCP ссылается на
`../external/<name>-mcp`. Их выносит отдельная задача.

Закреплено тестом `test_core_wheel_does_not_squat_generic_names`.

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

## Команды: каждый дистрибутив объявляет только свои

`uv tool install` кладёт в PATH скрипты **только запрошенного дистрибутива** —
скрипты зависимостей он не экспортирует (дословно из доки uv: «Executables
provided by dependencies of tool packages are not installed»). Поэтому команду
объявляет тот дистрибутив, которому принадлежит её код:

| Дистрибутив | Объявляет |
|---|---|
| `gc-cli` | `gc-cli` |
| `gc-tui` | `gc-ua` |
| `gc-server` | `gc-serve` |
| `gc-mcp` | `gc-mcp`, `gc-mcp-install` |
| `gc-ua` (мета) | все пять — через собственный лаунчер |
| `gc-core` | ничего, это библиотека |

### Почему имена не должны пересекаться

Не гигиена, а работоспособность: **uv владеет симлинком эксклюзивно**. Пока
`gc-mcp`/`gc-mcp-install` объявляли все три профиля, было так:

```
$ uv tool install "gc-cli @ ..."     # ок
$ uv tool install "gc-tui @ ..."
error: Executables already exist: gc-mcp, gc-mcp-install (use `--force` to overwrite)
```

Второй профиль не ставился вообще. С `--force` симлинки перехватывал последний,
а `uv tool uninstall gc-tui` уносил `gc-mcp` из PATH, хотя `gc-cli` остался
установлен и по-прежнему числил эту команду в своём receipt.

Прежняя редакция этого документа утверждала, что «пересечение симлинка
безвредно, какой профиль ни выиграл бы — команда отработает одинаково». Это
неверно: uv не перезаписывает, он **отказывает**. Проверено установкой.

Сейчас четыре дистрибутива ставятся подряд без единого `--force`, и снятие
любого не трогает чужие команды.

### Следствие для узкого профиля

`uv tool install "gc-cli @ ..."` даёт в PATH одну команду `gc-cli`. Менеджер
MCP при этом **установлен** — он жёсткая зависимость и физически лежит в venv
профиля (`tools/gc-cli/bin/gc-mcp` есть и запускается), но симлинка наружу нет.

Разница не наша, а модели `uv tool`. В обычном venv скрипты зависимостей
ставятся сами:

```
$ uv pip install gc-cli        →  bin/: gc-cli, gc-mcp, gc-mcp-install
$ uv tool install gc-cli       →  Installed 1 executable: gc-cli
```

`uv tool` изолирует инструменты и намеренно экспортирует только скрипты
запрошенного дистрибутива — так в доке uv: «Executables provided by dependencies
of tool packages are not installed».

Штатный способ получить их всё же — флаг:

```bash
uv tool install "gc-cli @ ...#subdirectory=packages/cli" --with-executables-from gc-mcp
```

Он резолвит `gc-mcp` из ТОГО ЖЕ коммита (через `[tool.uv.sources] workspace`,
в PyPI не идёт), пишется в `uv-receipt.toml` вместе с `from = "gc-mcp"` и
переживает `uv tool upgrade`. Набирается один раз. Именно эта форма и стоит в
командах установки в readme.

Декларативной формы у флага нет: ни переменной окружения, ни ключа в
`pyproject.toml` — только аргумент командной строки (в отличие от, скажем,
`--constraints`, у которого есть `UV_CONSTRAINT`). Ближайший аналог в pipx —
`--include-resources-from`; полного `--include-deps` у uv нет.

Ограничение флага: если ставить с ним ДВА профиля, второй снова столкнётся на
именах `gc-mcp*`. В таком случае менеджер ставится отдельным инструментом —
`uv tool install "gc-mcp @ ...#subdirectory=packages/mcp"`.

Закреплено тестами `test_each_distribution_declares_only_its_own_commands` и
`test_command_names_do_not_collide_between_leaf_distributions`.

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
внутри тестов берутся из `gc_testkit.paths`, а не собираются вручную: при
следующем переезде правится один файл, а не сорок.

Наборы тестов лежат при своих пакетах (`packages/*/tests`), а общая
инфраструктура — в dev-дистрибутиве `packages/testkit` (пакет `gc_testkit`).
В поставку он не входит; что он не протёк в колёса, проверяет
`test_testkit_never_ships`. Подробности — [Тесты](testing.md).

## Два корня, которые нельзя путать

| Функция | Что возвращает | Для чего |
|---|---|---|
| `bundled_assets.bundled_root()` | каталог ассетов: `packages/core/assets` в репо, `site-packages` в колесе | `prompts/`, `skills/`, `config.default.yml` |
| `bundled_assets.repo_root()` | корень рабочей копии либо **`None`** | `config_server.yml`, `integrations/` |

До разбиения на пакеты маркер `prompts/` лежал в корне репозитория, поэтому
`bundled_root()` возвращал сам корень — и на нём держались вещи, к ассетам
отношения не имеющие. После переезда ассетов смысл функции поменялся молча, и
все такие пути уехали в несуществующий `assets/scripts/…`. Ни один не падал
громко: `Popen` путь не проверяет, `subprocess.run(check=False)` код возврата не
смотрит, а поиск `config_server.yml` просто откатывался на дефолт — **вместе с
выключенной авторизацией REST**.

`repo_root()` возвращает `None` в установленном колесе намеренно: рабочей копии
там нет, и честный `None` заставляет вызывающего решить, что делать, вместо того
чтобы получить путь, которого не существует.

## Что сломается, если добавить модуль

Ничего: модуль кладётся в нужный пакет (`packages/<dist>/src/gc_*/`) и приезжает
в колесо автоматически — манифест забирает пакет целиком.

Внимания требуют **строковые** ссылки на модули. Кодмод правит `import`, но
модуль можно назвать и строкой: `monkeypatch.setattr("gc_core.x")`,
`sys.modules["gc_core.tools"]`, `f"gc_core.tools.{sub}"`. Около трети таких мест
при устаревшем имени не падают, а молча становятся no-op — цикл выселения
пустеет, заглушки протекают между тестами, набор зелёный. Проверка:

```bash
uv run python -m gc_testkit.module_literals
uv run pytest tests/test_packaging_wheel.py
```
