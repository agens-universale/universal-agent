# Changelog

## Unreleased

### Breaking — async-only ядро: одна форма на операцию

Синхронные двойники корутинных операций сняты. Ядро больше не заводит второй
event loop и не морозит loop вызывающего: портал (`gc_core.utils.portal`) и
`SyncApiInsideEventLoopError` удалены вместе с синхронными входами.

**Файловый порт `FsBackend` — ломающая правка для чужих реализаций.** 27
файловых методов стали корутинами, а `iter_lines`, `iter_walk`, `iter_files`,
`iter_walk_entries` — асинхронными генераторами (`async for`). Примесь
`AsyncFileOps` с двадцатью восемью двойниками (`aexists`, `aread`, …) снята:
их не звал никто. Адаптера для файловых операций НЕТ — чужой `FsBackend`
обязан стать корутинным. Флаг `bridges_to_loop` удалён: развилки «инлайн или
поток» больше нет.

Это закрывает настоящий дефект, а не только дублирование: у `HarborBackend`
`exists()` — это `test -e` по сети, и синхронная форма ждала его через
`run_coroutine_threadsafe(...).result()`. На loop самого Harbor поток
блокировал сам себя (проверено зависанием). Теперь ожидание нативное.

**Пробы возможностей требуют прогрева.** `has_find`, `has_grep`,
`grep_exclude_dir`, `grep_include` читают только КЭШ. Позовите
`await backend.awarm_capabilities()` один раз перед работой (`fs_find_in_files`
делает это сам) либо задайте `_cap_overrides`. Холодное чтение отказывает
`CapabilitiesNotWarmedError` — вместо прежней тихой заморозки loop на четырёх
запусках процесса подряд.

**Запуск процессов.** Свои бэкенды реализуют только `aexec` / `aexec_argv` /
`arun_python`. Синхронные имена остались на порте входом для ЧУЖИХ
реализаций — их ядро видит через `SyncExecAdapter`, — а умолчание отказывает
`SyncExecNotSupportedError`.

**MCP.** Снято: `get_all_tools`, `ToolsCache.get_tools`,
`_build_tools_registry`, синхронные `list_tools` / `call_tool` в контракте
`McpClientPort`, синхронный снос потоковых сессий
(`_PersistentSessionManager.close_all` / `.invalidate`). Вместо них —
`ToolsCache.aget_tools`, `alist_tools` / `acall_tool`, `aclose_all` /
`ainvalidate`. Синхронная форма осталась ровно у долгоживущего stdio: там
обмен блокирующий по существу, а закрытие зовёт `atexit`.

**Сборка набора тулов.** `build_tools_data` → `abuild_tools_data`;
`get_mcp_tools_data_for_config` → `aget_mcp_tools_data_for_config`.

**Хранилище тредов.** Сняты синхронные формы ФАСАДА `ThreadSession`:
`from_config`, `persist_batch`, `archive_and_compact_summary_payload`
(используйте `afrom_config`, `apersist_batch`,
`aarchive_and_compact_summary_payload`). КОНТРАКТ хранилища не тронут:
одиннадцать синхронных методов `ThreadStoreBase` на месте, обещание «своё
хранилище можно синхронное» в силе, рядом стоит `AsyncThreadStoreBase` для
`redis.asyncio` / `asyncpg`.

**Заголовки запроса.** `create_call_headers` разделён: `build_call_headers`
ничего не ждёт (чистая сборка), `acreate_call_headers` берёт токен. Прежнее
имя не сохранено алиасом — синхронная форма ходила в сеть за токеном двумя
блокирующими HTTP по 10 с таймаута, и делать это тихо под видом сборки
заголовков неверно.

**Состояние.** `State.__init__` больше не ходит в файловую систему. Если нужны
данные о проекте (`projectDir`, `isGitProject`, листинг) — строьте через
`await State.acreate(...)`; конструктор без них отдаёт пустой словарь.

**Субагенты.** Сняты синхронные `SubagentRunner.run` / `_run_internal`
(обёртки второго loop) и синхронный `persist_subagent_trace`. Последний умел
писать только в синхронное хранилище и на асинхронном отказывался варнингом —
то есть у встраивающего с redis трасса субагентов не писалась ни разу при
полностью работающих субагентах.

**Контекст прогона.** `create_run_context` снят, осталась
`acreate_run_context`.

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
