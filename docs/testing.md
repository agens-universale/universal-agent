[Назад к README](../readme.md)

# Тесты

Один раз в корне репо: `uv sync`. Подтягивает dev/test-зависимости (`textual`, `rich` — в основных deps; `office-mcp` из `external/office-mcp` — в test-группе).

`uv tool install -e .` и `gc-mcp-install` — для **запуска агента** (`gc-cli` / `gc-ua`), не для pytest. Тесты гоняйте через `uv run pytest`, а не из venv `uv tool`.

```bash
# весь репозиторий: наборы всех пакетов + корневые
uv run pytest

# office-mcp (свой venv через --directory)
uv run --directory external/office-mcp pytest

# science-mcp
uv run --directory external/science-mcp pytest

# computer-mcp (на Linux)
uv run --directory external/computer-mcp pytest -m "not windows"
```

Папка или файл: `uv run pytest packages/core/tests/security/ -q`

Живые MCP-серверы, настоящий браузер и реальная модель автоматическим набором
не покрываются — для них есть [ручные тесты](manual-tests.md).

## Наборы живут при пакетах

Каждый дистрибутив держит свои тесты рядом с исходниками:

| Каталог | Что проверяет | Тестов |
|---|---|---|
| `packages/core/tests` | ядро/SDK | 3285 |
| `packages/tui/tests` | TUI | 2250 |
| `packages/mcp/tests` | рецепты и менеджер MCP | 137 |
| `packages/server/tests` | server и swarm | 170 |
| `packages/cli/tests` | CLI | 93 |
| `tests` | репозиторий целиком: колёса, литералы модулей, принадлежность тестов | 21 |
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
cd packages/core && uv run --project ../.. pytest -q     # 3252 passed
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
- `gc_testkit.plugin` — подключается автоматически через entry point `pytest11`;
  отдельный `conftest.py` в пакете для этого не нужен.
- `gc_testkit.helpers`, `gc_testkit.intent_fixtures`, `gc_testkit.tui_support`,
  `gc_testkit.tui_fixtures`, `gc_testkit.harbor_fakes` — импортируемые хелперы.

Тестовые файлы **не импортируют друг друга**: каталогов `tests` теперь пять, и
общего имени, по которому они видны друг другу, не существует. Всё общее едет
через `gc_testkit`.
