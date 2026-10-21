# Changelog

## Unreleased

### Feature — плагины (экспериментально)

- Плагин — каталог в раскладке Claude Code: `.claude-plugin/plugin.json`, `skills/`, `commands/`, `agents/`, `hooks/hooks.json`, `.mcp.json`. Готовые плагины оттуда подключаются без переделки. Без настройки поведение агента не меняется: списки плагинов пусты.
- Подключение: `plugins.paths` (каталоги), `plugins.sources` (git: `owner/repo`, `ref`, `subdir`; клон в кэш один раз) и команда `gc-plugin install | list | enable | disable | update | uninstall` (постоянное хранилище, как у OpenHands; установленные плагины работают без правки конфига).
- Skills плагина видны агенту как `<плагин>:<skill>`; команды плагина — как skills `<плагин>:<команда>` и напрямую `/плагин:команда аргументы` (в TUI через `/skill`); субагенты из `agents/` становятся кастомными субагентами `subagent_<плагин>_<имя>` (имена инструментов Claude Code переводятся в имена агента); серверы из `.mcp.json` попадают в `mcp.servers`.
- Хуки (внешние команды, JSON на stdin): `SessionStart`, `UserPromptSubmit`, `PreToolUse` (запретить вызов или поменять аргументы), `PostToolUse`, `PostToolUseFailure`, `Stop`. Хуки вызываются до встроенной защиты и не могут её обойти; сбой или таймаут хука не останавливает агента. В durable-режиме (Temporal) хуки отключены.
- Новая секция конфига `plugins` (`paths`, `sources`, `hook_timeout_sec`); команда `gc-plugin` объявлена в дистрибутиве `gc-mcp`. Описание: `docs/plugins-playground.md`.
- Не проверялось в TUI, ACP и `gc-server`; инструкции `!`команда`` внутри команд плагина не выполняются.

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
