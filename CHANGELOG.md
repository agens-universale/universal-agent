# Changelog

## Unreleased

### Breaking — хранилище агента v2 (`AgentStore`)

- Контракт `gc_core.agent_store.AgentStore` асинхронный и по логическим именам: `read(kind, name) -> bytes | None`, `list(kind, prefix)`, `read_memory(project)`, `write_memory(project, text, expected_version=…)`. Виды: `prompt` (имена — `Prompt`), `instructions` (`L0`, `L1`), `subagent`, `skill`. Ядро больше не передаёт хранилищу путь: `FileAgentStore(settings)` вычисляет его сам по настройкам (с runtime-слоем фронтенда), как раньше ядро. Прежние `read_text`/`list_dir`/`AgentResource`/`DEFAULT_AGENT_STORE` удалены; `MemoryAgentStore` (testkit) и конформанс-набор `gc_testkit.agent_store_conformance` — под новый контракт.
- Память проекта — через хранилище агента: версия (sha256 текста) и запись с проверкой версии. Параллельная правка (второй инстанс, второе окно) больше не теряется: тул `project_memory` перечитывает память и применяет `append` к свежему тексту, повторный конфликт — ошибка тула `conflict`.
- Скрипты скиллов из своего хранилища агента запускаются: файлы скилла выкладываются через `os_backend` в `<launch_root>/.gc/skills_runtime/<слой>/<скилл>` (раньше — отказ).
- `L2` (`.gc/AGENTS.md`) и `L3` (`AGENTS.md` проекта) читаются из workspace, не из хранилища агента.
- `PromptPreparationRequest` и `StatePreparationRequest` (порты `prompt_loader`/`state_preparer`) больше не несут путей промптов и роли запуска: их знает хранилище исполнителя.

### Хранилища v2 — фронтенды читают и пишут треды через хранилище тредов

- TUI, CLI, сервер, gc-chat и gc-acp работают с тредами через фасад `Threads` поверх `ThreadStore`; формат файлов тредов прежний, новое только добавляется.
- gc-chat: ответ Responses — запись потока `chat.responses` треда, `response_id` — алиас треда. Новые файлы `responses-index/*.json` не пишутся, старые читаются.
- gc-acp: cwd и заголовок сессии — в треде (`frontend["acp"]`). Новые `acp-sessions/*.json` не пишутся, старые читаются до первого хода сессии.
- `/export log-json` собирает строки треда из хранилища (тот же конверт строки истории); строки, которые не читаются как запись истории, в выгрузку не попадают.
- Тред, который ведёт durable-исполнитель (Temporal), удаляет только он сам: уборщик сервера и `/sessions` в TUI его пропускают (TUI — с предупреждением).
- После удаления треда в `.store/` остаётся служебная запись с его поколением: по ней читатели замечают, что тред с тем же id создан заново.

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
