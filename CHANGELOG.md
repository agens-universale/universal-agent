# Changelog

## Unreleased

### Breaking — асинхронное ядро (переезд на asyncio)

I/O-хребет `gc_core` переведён на нативный `asyncio`. У каждой операции одна
реализация — асинхронная; синхронные точки входа сохранены и **не
депрекейтятся**: они стали тонкими шимами через приватный event loop в фоновом
потоке (`gc_core.utils.portal`). Подробности и правила выбора API —
[`docs/async.md`](docs/async.md).

- **Синхронный API из живого event loop теперь ошибка, а не тихая блокировка.**
  `AgentExecutor.run`, `run_agent`, `request_backend_*`, позванные изнутри
  работающего loop, поднимают `SyncApiInsideEventLoopError` с указанием на
  асинхронный двойник. Заблокировать чужой loop библиотека не имеет права.
- **Появился публичный async-API:** `gc_core.aio.AsyncAgent`,
  `AgentExecutor.arun`, `acreate_run_context`, `arun_agent`, `aapply_tool`,
  `arequest_backend_*`. Таблица соответствия — в `docs/async.md`.
- **`gc_server` больше не патчит `asyncio.run` процессно-глобально.** Патч
  действовал на весь процесс, включая код встраивающего приложения; дыру
  закрывает локальный `gc_core.utils.aio_compat.run_coro_blocking`.
- **Путь к модели переехал с `requests` на `httpx.AsyncClient`.** Прокси
  задаются через `mounts` по схемам, `trust_env=False` сохранён (иначе
  `~/.netrc` перетирает явный `Authorization`). Тесты, патчившие приватную
  `connect_backend._NO_NETRC_SESSION`, подменяют теперь фабрику клиента.
- **`exec_fetch_url`, `exec_simple_fetch_url`, `exec_download_file` — на
  `httpx`.** У них две точки входа: корутинная `acall` для цикла агента и
  синхронная `__call__` поверх неё, поэтому `apply_tool` продолжает работать.
  Прокси эти тулы берут из окружения через `resolve_proxies`.
- **`gc_server`: прогон агента стал `asyncio.Task` вместо потока на запрос.**
  Поле `ActiveRun.thread` сохранило имя, но теперь содержит таску.
- Потоков на вызов больше нет: exec — на `create_subprocess_exec`, MCP-stdio
  читает одним потоком на процесс вместо потока на строку.

### Fix — ревизия async-переезда

Сверка ветки с планом ([отчёт](docs/reports/2026-08-21-async-core-audit.md))
нашла и закрыла 22 дефекта. Наиболее заметные для пользователя:

- **`gc-cli --init` не работал вовсе** — падал с `SyncApiInsideEventLoopError`.
- **`turn_ask_human` в TUI не получал ответ пользователя**: диспетчер уводит
  синхронный тул в поток, где потоко-локальная подмена stdin не действует.
  TUI подключён к порту `ctx.ask_human`.
- **exec-тулы объявляли ложный таймаут и теряли вывод**, если команда
  оставляла фонового потомка; отмена прогона оставляла процесс-сироту.
- **Три подсистемы молча выключились** (pre-run компакция, сводка трассы при
  остановке по лимиту, финальная интент-запись): синхронный шим звался из
  корутины, а fail-open глотал ошибку.
- **TLS:** клиентский сертификат mTLS терялся при CA-бандле, заданном путём;
  разные CA-бандлы попадали в одну ячейку кэша клиентов; настроенный прокси
  отменял TLS-настройки вызывающего.
- **`AsyncAgent.aclose()` рвал запросы соседнего агента** на том же loop.
- **Утечка дескрипторов** на одноразовых MCP-вызовах.
- SSE портил ответ, если провайдер объявлял не-UTF-8 кодировку.

### Breaking — раздельная установка профилей через extras (0.2.3 → 0.3.0)

- **Базовая установка `gc-ua` больше не тянет TUI.** Кто ставил `uv tool install -e .`
  и ждал `gc-ua`, должен дописать extra: `uv tool install -e ".[tui]"` (или
  `uv tool install "gc-ua[tui] @ git+https://gitverse.ru/gigacode-ai/universal-agent"`).
  Extras умеют только добавлять зависимости, вычесть нельзя — поэтому дефолт
  сделан headless, иначе облегчённого профиля не существовало бы вовсе.
- Профили: `gc-ua` (ядро + CLI + MCP-рантайм) · `[tui]` — `textual`, `rich`,
  `pygments` · `[server]` — `fastapi`, `uvicorn[standard]`, `psutil` (swarm
  входит сюда же: он запускается только через `POST /swarm/run`) · `[mcp]` —
  пустой по зависимостям, заведён для явности профилей · `[all]`.
- `pygments` объявлен явно: его импортируют `tui/services/{trace_render,file_view}.py`,
  а в зависимостях он не значился и приезжал транзитивно через `rich`.
- `pytest` / `pytest-asyncio` убраны из runtime-зависимостей в
  `[dependency-groups] test` — раньше их получал каждый пользователь.
- Новая точка входа `gc-serve`. Заодно починен запуск сервера из установленного
  колеса: в `server.py` стояло `uvicorn.run("src.server:app", ...)`, но под
  колесом (`sources = ["src"]`) модуль называется `server`, и строка
  резолвилась только при запуске из корня репозитория.
- `server_requirements.txt` удалён: он пинил `uvicorn<0.31.0`, что несовместимо
  с `mcp==1.26.0` (требует `uvicorn>=0.31.1`). Конфликт не всплывал только
  потому, что файл ставили отдельно, мимо резолвера. Верхняя граница снята.
- `packaging/cli-only-excludes.txt` удалён — приём `uv tool install --excludes`
  вытеснен профилями.
- Точки входа `gc-ua` / `gc-serve` кладутся в PATH всегда (uv ставит все скрипты
  дистрибутива независимо от extras) и без своего профиля печатают однострочную
  подсказку в stderr с готовой командой, возвращая 1 — вместо трейсбека.
- Направление зависимостей приведено в порядок: `swarm/config.py` больше не
  импортирует `rest_api.server_config` (берёт `bundled_root()` из ядра), а
  `tui` / `swarm` обращаются к ядру только через публичные имена
  `config.default_config_path` / `project_local_config_path` /
  `bootstrap_user_config` и `onboarding.valid_endpoint_url` вместо подчёркнутых.
  Подчёркнутые имена сохранены, публичные — делегирующие обёртки, поэтому
  monkeypatch приватных имён в тестах продолжает работать.
- `create_run_context(is_tui=True)` переживает отсутствие пакета `diff_tui`
  (едет с `[tui]`): импорт под `try/except ImportError`, без diff-журнала
  прогон полностью работоспособен.

### Feature — limit trace summary (LIMIT_TRACE_SUMMARY_SPEC)

- When the main agent or a subagent hits a limit (`step_limit` / `tool_call_limit` / `token_limit`), the trace is now summarized by a single auxiliary LLM call and returned as a JSON envelope `{ok:false, status, message, raw_trace_summary}` instead of a flat string.
- `build_limit_envelope(state, reason, ctx)` in `src/summarize/limit_trace_summary.py` serializes `state.messages` (system prompt stripped; full trace, no truncation), calls `request_auxiliary_completion_text`, and returns the envelope. On LLM failure the envelope is still returned with an empty `raw_trace_summary`.
- The auxiliary LLM call does NOT count as `state.steps` and does NOT turn the agent loop (spec §5.3). Finish detection keeps priority over limit (spec §6.3).
- `SubagentRunner` limit branch is pure pass-through: it returns the child envelope unchanged (spec §6.2).
- Config kill-switch: `agent.limit_trace_summary` (default `true`). Весь трейс обрабатывается целиком, без искусственных лимитов на размер входа или выхода; `max_tokens` ответа берётся из `chat_completions.llm.max_tokens` (как у прочих aux-вызовов).
- Prompt: `prompts/limit_trace_summary.md`.
- Old spec `docs_md/done/2026-07-31-step-limit-forced-finish-spec.md` marked **superseded**.

### Breaking — tool namespaces (TOOL_NAMESPACES_SPEC v1.1)

- Builtin API names now use prefixes: `fs_*`, `exec_*`, `turn_*`, `session_*`, `project_*`; MCP `mcp_*`; subagent `subagent_*` (e.g. `subagent_special_agent`).
- Notable renames: `submit_solution` → `turn_finish`, `working_memory` → `session_memory`, `long_memory` → `project_memory`, `ask_human` → `turn_ask_human`.
- Config keys: `project_memory`, `strict_turn_finish_validation`, `intent_scope.tools` / `stop_tools` / `tool_usage_notice` use to-be tool names.
- Project file: `.gc/project_memory.md` (no automigrate from `long_memory.md`).
- No runtime alias for old tool or config names.

Internal unchanged: `state.shared.working_memory`, XML `<working_memory>` in summarizer payloads, yaml section `working_memory:` (summarize params).

Tool JSON envelopes now use to-be `"tool"` field names (e.g. `fs_read_file`, `exec_command`). `project_memory` domain API renamed (`build_project_memory_status`, `ProjectMemoryError`, …). Compaction prompt: `prompts/project_memory_compaction_prompt.md`.

See `docs_md/tools/TOOL_NAMESPACES_SPEC.md` and updated `docs_md/tools/SPEC.md`.
