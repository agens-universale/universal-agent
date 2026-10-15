# Changelog

## Unreleased

### Breaking — Temporal на контракте хранилища тредов v2

- Под Temporal ядро пишет тред через контракт `ThreadStore` v2, как локально: хранилище воркера — любое v2 (`FileThreadStore` из `gc_core.threads.file_store`, `SQLiteThreadStore` из `gc_stores.sqlite`, `PostgresThreadStore`, своё — проверка `gc_testkit.thread_store_conformance_v2`). Activities треда — `universal_agent.thread.{get,read_stream,read_archive,resolve_alias,commit,append_side,claim,release,delete}` вместо `load`/`commit`/`read_trace`.
- Удалены старый durable-контракт (`gc_core.thread_store_durable`: снимок, операция, квитанция), `DurableFileThreadStore`, `gc_core_temporalio.storage` (SQLite переехал в `gc-stores`, схема новая) и `gc_testkit.durable_thread_store_conformance`. Совместимость с историями и базами прежних воркеров не поддерживается (Temporal не в эксплуатации).
- Владелец треда — аренда v2 `LeaseRequest(owner="<workflow id>#<first_execution_run_id>", ttl=None)` (цепочка run): переживает Continue-As-New, передаётся субагентам; новый запуск с тем же workflow ID — новая эпоха. Аренду со сроком (локальный TUI/CLI) сессия не отбирает. `gc-core-temporalio` зависит от `gc-stores`.
- `SideChange.lease`: запись хода сбоку (статус, потоки, алиасы) в тред с бессрочной арендой проходит только по ней (`rules.check_side_writer`, все хранилища v2); состояние фронтенда — без аренды.
- `persist: turn` включается настройкой (под Temporal), а не способностью хранилища; компакция под Temporal — две записи (`replace_history`, затем состояние), как локально.
- Квитанция `commit` во всех хранилищах v2 несёт аренду после записи (`reset_history` её снимает): пишущий не берёт аренду заново перед каждой записью.

### Добавлено — `PostgresThreadStore` (дистрибутив `gc-stores`)

- Новый дистрибутив `gc-stores` (библиотека, в `gc-ua` не входит): хранилища тредов на базах. `gc_stores.postgres.PostgresThreadStore` — контракт `ThreadStore` v2 на Postgres для нескольких инстансов на одной базе; драйвер — extra `gc-stores[postgres]` (asyncpg). Таблицы создаёт `await store.ensure_schema()`; подключение — `AsyncAgent.create(settings, thread_store=store)`.
- Конформанс-наборы хранилищ тредов (`thread_store_conformance_v2.assert_conforms`, `thread_store_conformance.assert_conforms_async`) закрывают хранилище после каждой проверки (`await store.close()`, если метод есть) — для хранилищ с пулом соединений. Новые проверки: явно заданные поля моделей (`model_fields_set`) переживают хранилище — по ним строится meta; `NaN`/`Infinity` в данных пишутся и читаются, как у файлового хранилища.

### Breaking — хранилище агента v2 (`AgentStore`)

- Контракт `gc_core.agent_store.AgentStore` асинхронный и по логическим именам: `read(kind, name) -> bytes | None`, `list(kind, prefix)`, `read_memory(project)`, `write_memory(project, text, expected_version=…)`. Виды: `prompt` (имена — `Prompt`), `instructions` (`L0`, `L1`), `subagent`, `skill`. Ядро больше не передаёт хранилищу путь: `FileAgentStore(settings)` вычисляет его сам по настройкам (с runtime-слоем фронтенда), как раньше ядро. Прежние `read_text`/`list_dir`/`AgentResource`/`DEFAULT_AGENT_STORE` удалены; `MemoryAgentStore` (testkit) и конформанс-набор `gc_testkit.agent_store_conformance` — под новый контракт.
- Память проекта — через хранилище агента: версия (sha256 текста) и запись с проверкой версии. Параллельная правка (второй инстанс, второе окно) больше не теряется: тул `project_memory` перечитывает память и применяет `append` к свежему тексту, повторный конфликт — ошибка тула `conflict`.
- Скрипты скиллов из своего хранилища агента запускаются: файлы скилла выкладываются через `os_backend` в `<launch_root>/.gc/skills_runtime/<слой>/<скилл>/<версия>` и сверяются с хранилищем перед каждым запуском (раньше — отказ).
- `L2` (`.gc/AGENTS.md`) и `L3` (`AGENTS.md` проекта) читаются из workspace, не из хранилища агента.
- `PromptPreparationRequest` и `StatePreparationRequest` (порты `prompt_loader`/`state_preparer`) больше не несут путей промптов и роли запуска: их знает хранилище исполнителя.

### Хранилища v2 — фронтенды читают и пишут треды через хранилище тредов

- TUI, CLI, сервер, gc-chat и gc-acp работают с тредами через фасад `Threads` поверх `ThreadStore`; формат файлов тредов прежний, новое только добавляется.
- gc-chat: ответ Responses — запись потока `chat.responses` треда, `response_id` — алиас треда. Файлы `responses-index/*.json` старые читаются, а новые пишутся вместе с потоком — откат на develop видит ответы ветки; двойная запись уйдёт в следующем релизе.
- gc-acp: cwd и заголовок сессии — в треде (`frontend["acp"]`). Файлы `acp-sessions/*.json` читаются, пока у треда нет этого состояния, и пишутся вместе с ним (создание, ход, удаление) — для отката на develop; двойная запись уйдёт в следующем релизе. Сессия develop без единого хода снова загружается (`session/load`); одна нечитаемая сессия не роняет `session/list`.
- `/export log-json` собирает строки треда из хранилища (тот же конверт строки истории); строки, которые не читаются как запись истории, в выгрузку не попадают.
- Тред, который ведёт durable-исполнитель (Temporal), удаляет только он сам: уборщик сервера и `/sessions` в TUI его пропускают (TUI — с предупреждением).
- После удаления треда в `.store/` остаётся служебная запись с его поколением и эпохой аренды: по ней читатели замечают, что тред с тем же id создан заново. В списке тредов её нет, через сутки её убирает удаление следующего треда или уборщик сервера (`FileThreadStore.sweep_tombstones`).
- Квитанции записи (`.store/<h>/receipts/`) тред чистит сам при записи, раз в сутки — старше недели; без сервера `.store/` больше не растёт без предела.
- Аренда треда держится только на время хода (продлевается в фоне, снимается после): второй инстанс на другом хосте ведёт тред сразу. Отказ хранилища в записи шага (тред ведёт другой инстанс) завершает ход ошибкой, а не теряет шаги.
- Тред, удалённый кодом develop после отката, не оживает по оставшимся потокам фронтендов и алиасам; удаление треда сносит потоки по точным именам, не задевая тред с похожим id.

### Feature — limit trace summary (LIMIT_TRACE_SUMMARY_SPEC)

- When the main agent or a subagent hits a limit (`step_limit` / `tool_call_limit` / `token_limit`), the trace is now summarized by a single auxiliary LLM call and returned as a JSON envelope `{ok:false, status, message, raw_trace_summary}` instead of a flat string.
- `build_limit_envelope(state, reason, ctx)` in `src/summarize/limit_trace_summary.py` serializes `state.messages` (system prompt stripped; full trace, no truncation), calls `request_auxiliary_completion_text`, and returns the envelope. On LLM failure the envelope is still returned with an empty `raw_trace_summary`.
- The auxiliary LLM call does NOT count as `state.steps` and does NOT turn the agent loop (spec §5.3). Finish detection keeps priority over limit (spec §6.3).
- `SubagentRunner` limit branch is pure pass-through: it returns the child envelope unchanged (spec §6.2).
- Config kill-switch: `agent.limit_trace_summary` (default `true`). Весь трейс обрабатывается целиком, без искусственных лимитов на размер входа или выхода; `max_tokens` ответа берётся из `chat_completions.auxiliary_llm.max_tokens` (как у прочих aux-вызовов).
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
