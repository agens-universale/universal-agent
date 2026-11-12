[Назад к README](../readme.md)

# Тесты

Один раз в корне репо: `uv sync`. Подтягивает dev/test-зависимости (`textual`, `rich` — в основных deps; `office-mcp` из `packages/mcp/external/vendor/office-mcp` — в test-группе).

`uv tool install -e .` и `gc-mcp-install` — для **запуска агента** (`gc-cli` / `gc-ua`), не для pytest. Тесты гоняйте через `uv run pytest`, а не из venv `uv tool`.

```bash
# весь репозиторий: наборы всех пакетов + корневые
uv run pytest

# office-mcp (свой venv через --directory)
uv run --directory packages/mcp/external/vendor/office-mcp pytest

# science-mcp
uv run --directory packages/mcp/external/vendor/science-mcp pytest

# computer-mcp (на Linux)
uv run --directory packages/mcp/external/vendor/computer-mcp pytest -m "not windows"
```

Папка или файл: `uv run pytest packages/core/tests/security/ -q`

## Наборы живут при пакетах

Каждый дистрибутив держит свои тесты рядом с исходниками:

| Каталог | Что проверяет | Тестов |
|---|---|---|
| `packages/core/tests` | ядро/SDK | 4069 |
| `packages/tui/tests` | TUI | 3381 |
| `packages/server/tests` | server и swarm | 216 |
| `packages/mcp/tests` | рецепты и менеджер MCP | 159 |
| `packages/cli/tests` | CLI | 129 |
| `packages/temporalio/tests` | Temporal (живые — при `temporal` CLI) | 303 |
| `packages/stores/tests` | хранилища на базах (Postgres — при `GC_TEST_POSTGRES_DSN`) | 29 |
| `tests` | репозиторий целиком: колёса, литералы модулей, принадлежность тестов | 66 |
| `external/litellm/tests` | батарейка LiteLLM Proxy | 13 |

Принадлежность закреплена тестом `tests/test_test_ownership.py`: набор пакета не
имеет права импортировать пакеты выше себя по зависимостям. Без него «зелёный
автономный прогон» ничего не доказывал — чужой пакет всё равно стоит в общем
рабочем окружении, и импорт резолвился (ревизия 18.08.2026 нашла три таких
файла в наборе ядра).

Файл принадлежит самому «верхнему» пакету из тех, что импортирует: фронтенд
важнее ядра, потому что зависит от него.

Набор пакета самодостаточен, и это проверяемое утверждение, а не пожелание:

```bash
cd packages/core && uv run --project ../.. pytest -q
```

Если ядро случайно потянет TUI или server, эта команда покраснеет — при общем
наборе такую зависимость не видно.

## Общая инфраструктура: `gc-testkit`

Раскладка исходников, фикстуры и хелперы, нужные наборам сразу нескольких
пакетов, лежат в отдельном dev-дистрибутиве `packages/testkit` (пакет
`gc_testkit`). Он ставится только в рабочее окружение и в колёса не попадает —
это закреплено тестом `test_testkit_never_ships`.

- `gc_testkit.paths` — `REPO`, `ASSETS`, `PACKAGE_SRC_DIRS`, `module_dir()`.
  **Пути к корню репозитория считайте только отсюда.** Идиома
  `Path(__file__).resolve().parents[N]` ломается при любом переезде файла, причём
  молча: если каталог по ошибочному пути не существует, `rglob` вернёт пустоту и
  проверка пройдёт вхолостую.
- `gc_testkit.plugin` — подключается автоматически через entry point `pytest11`,
  но бутстрап включает только для НАШИХ наборов: гейт сверяет `rootdir`
  собираемого набора с корнем рабочей копии;
  отдельный `conftest.py` в пакете для этого не нужен.
- `gc_testkit.helpers`, `gc_testkit.intent_fixtures`, `gc_testkit.tui_support`,
  `gc_testkit.tui_fixtures`, `gc_testkit.harbor_fakes` — импортируемые хелперы.
- Хранилища: `memory_thread_store_v2` / `memory_agent_store` — образцы в
  памяти, `thread_store_conformance_v2` / `agent_store_conformance` —
  конформанс-наборы, которыми проверяются и наши хранилища (файлы, SQLite,
  Postgres), и чужие (`assert_conforms`).

В тест-ките лежит только то, что нужно наборам НЕСКОЛЬКИХ пакетов и не зависит от
их файлов. `gc_testkit.tui_support` — три функции про экраны вообще (`wait_until`,
`open_settings_section`, `settings_section_index`). Монтаж конкретных форм, их фейки
и изоляторы живут в наборе TUI, в `packages/tui/tests/form_harness.py`: они лениво
импортируют соседние тест-модули, и в общем дистрибутиве это перевернуло бы
зависимость — тест-кит стал бы зависеть от тестовых файлов одного пакета.

**Через границу пакетов тестовые файлы друг друга не импортируют**: каталогов
`tests` пять, общего имени, по которому они видны друг другу, нет — всё общее едет
через `gc_testkit`. ВНУТРИ одного набора соседний импорт по голому имени работает и
используется (`form_harness` тянет `test_settings_screen`, ядро — `from
test_project_data_backend import FakeSandboxBackend`): pytest кладёт каталог набора
на `sys.path`.

## Где подменять `managed_config_path`

Точка подмены зависит от того, что изолируете, и это не произвол, а отражение того,
кто как связывает имя. Дом функции один — `gc_core.config_modules.env_vars`.

| Что изолируете | Что подменять |
|---|---|
| `load_settings` (загрузка настроек) | `gc_core.config_modules.env_vars.managed_config_path` — `config.py` зовёт его через модуль |
| перекрытия в `/settings` (`config_origin`) | `gc_core.config_modules.env_vars.managed_config_path` — он импортирует лениво |
| запись managed-слоя рецептами | `recommended_mcp.runtime.wiring.managed_config_path` |

Первую строку легко перепутать со второй: подмена «дома» до `load_config` не
доезжает, а тест при этом ЗЕЛЁНЫЙ — просто читает настоящий `~/.gc/config.local.yml`
запускающего. Десять таких подмен так и жили, пока реэкспорт в `mcp_runtime.paths`
не убрали и они не упали громко.

## Проверки качества

Линтер, форматтер, порог когнитивной сложности и локальный аналог правил
Sonar живут в одном гейте — `scripts/lint.sh` (внутри pre-commit). Как
поставить хуки, что и где проверяется и как глушить находки — в
[CONTRIBUTING.md](../CONTRIBUTING.md#качество-кода).

## Temporal

Живые тесты `packages/temporalio/tests/*_live.py` поднимают dev-сервер
Temporal через `temporal` CLI. Без CLI они пропускаются; прогон, который
обязан проверить Temporal (перед слиянием, правки ядра), запускайте с
`REQUIRE_TEMPORAL=1` — тогда отсутствие CLI будет ошибкой, а не пропуском:

```bash
REQUIRE_TEMPORAL=1 uv run pytest packages/temporalio/tests
```

Тред под Temporal пишется через провод хранилища v2 (прокси workflow →
activities воркера): разницы, куски и повторы после записи проверяет
`test_thread_store.py` без сервера, число activities треда на шаг закреплено
в `test_workflow_live.py`.

Новый ввод-вывод ядра без решения для Temporal ловят тесты без CLI:
`test_port_contract.py` (каждый необязательный атрибут контекста и порт —
привязан в Temporal или объяснён) и проверка полноты в `test_parity_live.py`
(каждый эталонный сценарий ядра гоняется под Temporal или стоит в списке
пропусков с причиной). В коде ядра без порта используйте
`gc_core.execution.ports.port(ctx, "<имя>")`: под Temporal отсутствие порта —
отказ хода, а не локальный запасной путь в коде workflow.

## Postgres

Тесты `packages/stores/tests` берут базу из `GC_TEST_POSTGRES_DSN`; без
переменной тесты с базой пропускаются, `REQUIRE_POSTGRES=1` делает её
отсутствие ошибкой. Каждое хранилище в тестах живёт на своей схеме
(`gc_test_<id>`), схемы удаляются в конце теста — подойдёт любая база, где
пользователю можно создавать схемы.

```bash
# временный кластер (Homebrew Postgres), только TCP
initdb -D /tmp/gc-pg -U postgres --auth=trust -E UTF8
pg_ctl -D /tmp/gc-pg -o "-p 55432 -c unix_socket_directories='' -c listen_addresses=127.0.0.1" -l /tmp/gc-pg.log start
GC_TEST_POSTGRES_DSN=postgresql://postgres@127.0.0.1:55432/postgres REQUIRE_POSTGRES=1 \
    uv run pytest packages/stores/tests
```

Набор: конформанс контракта v2 (`assert_conforms`) и старый асинхронный набор
через порт сессии (путь, которым ходит агент), плюс то, чего конформанс не
видит: два инстанса на одной базе, данные байт в байт, часы базы, схема у
пользователя без права `CREATE`.

## Windows

Набор обязан быть зелёным и на Windows. CI нет, поэтому перед слиянием
изменений, которые трогают пути, файлы, процессы или замеры цикла, гоняйте набор
там. В PowerShell, из корня репо:

```powershell
uv run pytest -p no:cacheprovider -rfE --junitxml=win.xml
```

Личный конфиг запускающего тесты не видят на любой ОС: плагин тест-кита
подставляет на время прогона пустой `GC_HOME`.

Что тест должен учитывать, чтобы не краснеть на Windows. Общие механизмы лежат в
`gc_testkit`, свои заплатки по тестам не пишите:

- **Команда для оболочки.** `exec` на Windows идёт через cmd.exe: нет `sleep`,
  `yes | head`, `$VAR`, `>&2`, `python3`, одинарных кавычек `shlex.quote`, а
  многострочный `-c` обрезается на первом переводе строки. Команду собирайте
  через `gc_testkit.helpers.crossplatform.shell_command(argv)` или
  `python_command(tmp_path, script, *args)` (скрипт кладётся файлом). Метасимволы
  cmd.exe (`& | < > ^ % "`) в аргументах хелпер отклоняет на любой ОС — такой
  код идёт в скрипт.
- **Концы строк.** `write_text` на Windows пишет `\r\n`, `print` у потомка тоже.
  Фикстуры, где важны байты (размер, sha256, сравнение с фейком), пишите
  `write_bytes` или `write_text(..., newline="")`; вывод сверяйте через
  `.rstrip("\r\n")`.
- **Пути.** Сверяйте `Path`, `os.path.normcase` или `Path(...).as_posix()`, а
  не подстроку с `/`. `/opt/x` на Windows — путь от корня текущего диска.
- **Симлинки.** Без режима разработчика Windows отказывает (WinError 1314):
  первой строкой теста `require_symlinks(tmp_path)` — пропуск только там, где ОС
  действительно не даёт.
- **Таймер.** `asyncio.sleep` на Windows просыпается шагами по 15,6 мс. Критерий
  «loop свободен» — только через `assert_loop_was_free`, он учитывает
  `LOOP_TIMER_QUANTUM`; свой пульс — на `time.perf_counter`, не `monotonic`.
- **Нативный поиск.** На Windows рабочего `find | xargs | grep` нет, движок
  уходит в Python. Тесты, утверждающие `search_backend == "grep"`, помечайте
  `gc_testkit.helpers.native_search.requires_native_search`.
- **Только POSIX.** Swarm на Windows пропускается целиком (`conftest.py` набора
  server). Остальное POSIX-only (сигналы групп, `mkfifo`, опрос stdin, бит +x) —
  `skipif(os.name == "nt", reason=...)` с причиной, и только если поведение
  на Windows действительно не обещано.
