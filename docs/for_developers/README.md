[Назад к README](../../readme.md)

# Для разработчиков

Практическая страница для тех, кто пишет код universal-agent: как поднять
окружение, где что лежит, как запустить агента из исходников, какой путь
проходит изменение от правки до слияния и на чём уже спотыкались. Правила
участия — ветки, требования к PR, лицензия — в [CONTRIBUTING.md](../../CONTRIBUTING.md);
здесь — как с этим работать каждый день.

## Быстрый старт

Нужны [uv](https://docs.astral.sh/uv/) ≥ 0.9 и git. Python нужной версии uv
поставит сам (`.python-version`).

```bash
git clone git@gitverse.ru:gigacode-ai/universal-agent.git
cd universal-agent
git checkout develop

uv sync                        # зависимости, тесты, инструменты проверок
uv run pre-commit install      # хуки проверок на commit и push — один раз на клон

uv run pytest -q packages/core/tests/tools/fs   # быстрый смоук, ~20 секунд
scripts/lint.sh                                 # все проверки качества, как в CI
```

Если оба последних шага зелёные — окружение готово.

## Где что лежит

uv-workspace из шести пакетов. Ядро — SDK, фронтенды от него зависят, но не
наоборот: у `gc_core` нет ни одного импорта `gc_cli`, `gc_tui` или `gc_server`.

| Каталог | Пакет | Что внутри |
| --- | --- | --- |
| `packages/core/src/gc_core` | gc-core | ядро агента: цикл, тулы, бэкенды, конфиг, треды |
| `packages/core/assets` | gc-core | промпты, встроенные скиллы, шаблон `config.default.yml` |
| `packages/cli/src/gc_cli` | gc-cli | команда `gc-cli`, мастер первого запуска |
| `packages/tui/src/gc_tui` | gc-tui | текстовый интерфейс `gc-ua` на Textual, голосовой ввод |
| `packages/server/src/gc_server` | gc-server | HTTP-сервер `gc-serve` на FastAPI и swarm-режим |
| `packages/mcp/src/recommended_mcp` | gc-mcp | рецепты рекомендуемых MCP-серверов, установка и автозапуск |
| `packages/testkit/src/gc_testkit` | gc-testkit | общая тестовая инфраструктура, только для разработки |
| `packages/<пакет>/tests` | — | тесты пакета |
| `tests/` | — | тесты всего репозитория: сборка колёс, граница импортов, владение тестов |
| `external/`, `packages/*/external/` | — | вендорный код — не править и не форматировать |
| `integrations/` | — | стенды бенчмарков (Harbor) и их конфиги |
| `scripts/` | — | `lint.sh`, чекеры качества, `bump_version.py` |

С чего начать чтение ядра:

| Модуль | Роль |
| --- | --- |
| `gc_core/agent_executor.py` | исполнитель: `AgentExecutor.arun` — вход в один ход агента, очередь сообщений, стоп |
| `gc_core/processing_responses.py` | цикл агента: запрос к модели, разбор ответа, вызов тулов, финиш хода |
| `gc_core/run_context.py` | `RunContext` — всё, что нужно прогону: конфиг, бэкенд, реестры, логгер |
| `gc_core/tools/registry.py` | явный реестр встроенных тулов; новый тул регистрируется здесь |
| `gc_core/tools/fs`, `tools/exec`, `tools/session`, `tools/turn` | сами тулы по группам |
| `gc_core/backends/` | где исполняются файловые операции и команды: локально или в песочнице Harbor |
| `gc_core/config.py`, `config_modules/` | загрузка и слои конфига, схема, профили |
| `gc_core/utils/connect_backend.py` | HTTP к модели: стрим, ретраи, заголовки |
| `gc_core/security/` | гарды: чтение, запись, модерация команд |
| `gc_core/thread_store*.py` | хранение тредов |

Почему раскладка именно такая и какие правила из неё следуют —
[module-split.md](../module-split.md); как из неё собираются дистрибутивы —
[packaging.md](../packaging.md); почему всё асинхронное — [async.md](../async.md).

## Запуск из исходников

`uv sync` ставит пакеты рабочей копии в режиме редактирования: правка в
`packages/*/src` видна сразу, переустанавливать ничего не нужно.

```bash
uv run gc-ua                                                   # TUI
uv run gc-cli --config demo_config_openai.yml -q "привет"      # разовый запрос
uv run python -m gc_server.server                              # HTTP-сервер, порт 8000
```

* **Конфиг** по умолчанию — `~/.config/gc/config.yml` (Windows: `~/.gc/config.yml`),
  создаётся при первом запуске. `--config <файл>` берёт другой; в корне репо
  лежат примеры `demo_config*.yml`. Всё про ключи — [configuration.md](../configuration.md).
* **Отдельная «песочница» для экспериментов** — `GC_HOME=/tmp/gc uv run gc-ua`:
  конфиг, состояние, данные и кэш агента уйдут в `/tmp/gc`, ваш рабочий
  `~/.config/gc` не тронется. Точечно — `GC_CONFIG_DIR`, `GC_STATE_DIR`,
  `GC_DATA_DIR`, `GC_CACHE_DIR`.
* **Логи** пишутся в каталог, из которого запущена команда (и при `GC_HOME` тоже):
  `agent_main_<trace>_<run_id>.log`. Уровень и формат — `logging.log_level`
  (`debug` для отладки) и `logging.format` (`text` или `jsonl`) в конфиге.
* **Отладка MCP** — `MCP_DEBUG=1`: транспорт печатает сырой обмен с сервером.
* **`gc-serve --help` не запускать:** аргументы он не разбирает и сразу
  поднимает сервер без авторизации на `0.0.0.0:8000`.

Тесты гоняются через `uv run pytest`, а не из окружения `uv tool install`:
установленная командой `uv tool` копия агента — отдельный venv, в нём нет
тестовых зависимостей и не видно ваших правок.

## Путь изменения

1. **Ветка** от свежего `develop`: `feat/<имя>`, `fix/<имя>`, `chore/<имя>`.
2. **Правка и тест.** Новое поведение — с тестом; исправление ошибки — с тестом,
   который падал до исправления.
3. **Коммит.** Хук `pre-commit` проверяет изменённые файлы: гигиену, ruff
   (линт и формат), сложность, правила Sonar, мёртвый код. Если ruff что-то
   поправил сам, коммит остановится — посмотрите правки, `git add`, повторите.
   Формат сообщения — `тип(область): что` по-русски, подробнее в
   [CONTRIBUTING.md](../../CONTRIBUTING.md), раздел «Коммиты».
4. **Push.** Хук `pre-push` ищет дублирование кода по всему прод-коду (~45 с).
5. **Перед запросом на слияние** — `scripts/lint.sh` и `uv run pytest -q`
   целиком (около 11 тысяч тестов, ~20 минут).
6. **Запрос на слияние** — только в `develop`, на gitverse.

## Проверки качества — коротко

| Проверка | Когда | Что делать, если не пропускает |
| --- | --- | --- |
| ruff check | commit | чаще всего исправляет сам; остальное — по тексту ошибки |
| ruff format | commit | исправляет сам — добавьте файл и повторите коммит |
| complexipy, сложность ≤ 15 | commit | разбейте функцию на хелперы; хук называет функцию и её сложность |
| sonar_like_check | commit | повтор строки → константа; лишний класс в `except` → убрать и т. д. |
| мёртвый код (vulture) | commit | удалить; если живо неявно — в `scripts/dead_code_whitelist.py` с пояснением |
| дублирование (pylint) | push | вынести общее в функцию или базовый класс |

Область: линтер — весь код, кроме тестов; форматтер — весь код, включая
тесты; остальные проверки — прод-код всех пакетов, как у Sonar. Заглушки —
только с обоснованием в той же строке (`# noqa: КОД  # почему`,
`# NOSONAR: почему`). Устройство гейта, соответствие правилам Sonar и что
выключено — [code-quality.md](../code-quality.md); разбор последнего отчёта
Sonar — [sonar-report.html](../sonar-report.html).

## Тесты — коротко

* Наборы лежат при пакетах: `packages/core/tests`, `packages/tui/tests` и т. д.
  Набор пакета не импортирует пакеты «выше» себя — это проверяет
  `tests/test_test_ownership.py`.
* Общие фикстуры и фейки — в `gc_testkit`: фейковый Harbor-бэкенд, пути к
  исходникам, pytest-плагин.
* TUI проверяется через Textual `run_test()` и снапшоты (`__snapshots__`).
  Снапшотные тесты помечены на перезапуск (`flaky(reruns=2)` в
  `packages/tui/tests/conftest.py`): пара из них может пройти со второй попытки.

Подробно — [testing.md](../testing.md).

## Грабли, на которых уже стояли

* **`git fetch` не приносит `develop`.** В некоторых клонах `origin` настроен
  загружать только отдельные ветки. Тяните явно:
  `git fetch origin +refs/heads/develop:refs/remotes/origin/develop`.
  По той же причине после `git push` ссылка `origin/<ветка>` может остаться
  старой — не делайте `git reset --hard origin/<ветка>`, не сверившись с
  `git ls-remote origin`.
* **Декоратор уехал вместе с кодом.** Вынося тело функции в хелпер, проверьте,
  на какой функции остались декораторы. Однажды `@retry_with_backoff_async`
  переехал на внутренний хелпер, и запросы к модели молча перестали
  повторяться. Теперь эту границу держит `test_connect_backend_retry_boundary`.
* **Автофикс удаляет «лишний» импорт, который и был тестом.** `import` внутри
  `pytest.raises(...)` для ruff выглядит неиспользуемым. Поэтому линтер не
  проверяет тесты — только форматирует.
* **Имя модуля живёт и в строках.** `monkeypatch.setattr("пакет.модуль.имя", …)`,
  `sys.modules[...]` — при переносе модуля такие места не падают сами, а
  тихо перестают что-либо подменять. Страхуют `tests/test_import_health.py` и
  `tests/test_module_literals.py`.
* **Синхронный двойник асинхронной функции.** Ядро держит одну форму на
  операцию; новые пары `foo` / `afoo` в одном модуле ловит
  `tests/test_sync_async_pairs.py`.
* **Снимок сложности не пересоздают «чтобы прошло».** `complexipy-snapshot.json`
  — замороженный долг tui/server/mcp/testkit; он сужается сам, когда функцию
  упрощают. Новая сложная функция в него не добавляется — её разбивают.
* **Массовое переформатирование ломает `git blame`.** Его делают отдельным
  коммитом и добавляют хеш в `.git-blame-ignore-revs`.

## Что читать дальше

| Тема | Документ |
| --- | --- |
| Правила участия, ветки, PR | [CONTRIBUTING.md](../../CONTRIBUTING.md) |
| Проверки качества и Sonar | [code-quality.md](../code-quality.md), [sonar-report.html](../sonar-report.html) |
| Тесты | [testing.md](../testing.md) |
| Архитектура и границы пакетов | [module-split.md](../module-split.md), [packaging.md](../packaging.md) |
| Асинхронное ядро | [async.md](../async.md) |
| Конфигурация | [configuration.md](../configuration.md) |
| Хранилище тредов | [thread-store.md](../thread-store.md) |
| Политики безопасности (гарды, модерация команд) | [rootbox.md](../rootbox.md) |
| Сабагенты, скиллы | [subagents.md](../subagents.md), [skills.md](../skills.md) |
| Сервер и swarm | [server.md](../server.md), [swarm.md](../swarm.md) |
