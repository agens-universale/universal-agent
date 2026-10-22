# Changelog

## Unreleased

### Feature — плагины (экспериментально)

- Плагин — каталог в раскладке Claude Code (`.claude-plugin/plugin.json`, `skills/`, `commands/`, `agents/`, `hooks/hooks.json`, `.mcp.json`) или Agent Plugins (agent-plugins.org: `plugin.json` в корне, `skills/`, `mcp.json`). Формат определяется по манифесту (`.claude-plugin/plugin.json` или его отсутствие: Claude Code; только корневой `plugin.json`: Agent Plugins, строго: `$schema` 1.0.0 и `name` по шаблону, неверный плагин не загружается, неизвестные поля игнорируются с предупреждением, каталоги skills за пределами корня пропускаются; из Agent Plugins берутся только `skills/` и `mcp.json`, а `hooks/hooks.json`, `commands/`, `agents/` игнорируются). Плагины в стандартной раскладке подключаются без переделки. Без настройки поведение агента не меняется: списки плагинов пусты.
- Подключение: `plugins.paths` (каталоги), `plugins.sources` (git: `owner/repo`, `ref`, `subdir`; клон в кэш один раз, только при загрузке настроек или командой `gc-plugin`; цикл агента читает кэш и один раз предупреждает, если источник ещё не склонирован) и команда `gc-plugin install | list | enable | disable | update | uninstall` (постоянное хранилище; установленные плагины работают без правки конфига).
- Skills плагина видны агенту как `<плагин>:<skill>`; команды плагина — как skills `<плагин>:<команда>` и напрямую `/плагин:команда аргументы` (в TUI через `/skill`); субагенты из `agents/` становятся кастомными субагентами `subagent_<плагин>_<имя>` (имена инструментов Claude Code переводятся в имена агента); серверы из `.mcp.json` (или `mcp.json` без точки; `.mcp.json` главнее) попадают в `mcp.servers`; битый сервер пропускается с предупреждением (значения в лог не пишутся), конфиг грузится.
- Хуки (внешние команды, JSON на stdin): `SessionStart`, `UserPromptSubmit`, `PreToolUse` (запретить вызов или поменять аргументы), `PostToolUse`, `PostToolUseFailure`, `Stop`. Хуки вызываются до встроенной защиты и не могут её обойти; сбой или таймаут хука не останавливает агента. В durable-режиме (Temporal) хуки отключены. Матчер инструмента сверяется с именем агента или с именем Claude Code (`Bash`, `Edit`); в `tool_name` уходит имя Claude Code, если оно есть, а `tool_input` остаётся в схеме аргументов агента. `SessionStart` срабатывает один раз на тред, если хук вернул контекст; `SessionStart`, `UserPromptSubmit` и `Stop` в субагентах не запускаются (хуки инструментов работают). Хукам заданы `GC_PLUGIN_ROOT` и `CLAUDE_PLUGIN_ROOT`. По таймауту и остановке убивается вся группа процессов хука; на Windows используется `bash` из PATH (нет: предупреждение, хук пропускается).
- `mcp.json` Agent Plugins читается строго по спеке (`$schema`, `stdio`/`streamable-http`/`sse`, `${PLUGIN_ROOT}` и `${PLUGIN_DATA}` только в `args`/`env`/`cwd`, `PLUGIN_ROOT`/`PLUGIN_DATA` в окружении stdio, данные в `<gc_data>/plugins/data/<имя>`, HTTPS вне loopback); в `.mcp.json` `type: http` и `url` без `type` используют streamable HTTP; `sse` в обоих форматах подключается как `stream_sse`.
- `gc-plugin install|update` показывают хуки и команду или url MCP как в файлах плагина (`${VAR}` и `${CLAUDE_PLUGIN_ROOT}`/`${PLUGIN_ROOT}` не раскрываются, пропущенные серверы MCP указаны с причиной) и спрашивают `Установить? [y/N]` (`--yes`/`-y`; без TTY и без `--yes` отказ; `update` спрашивает, только если хуки или MCP изменились). TUI: `/plugins` показывает список плагинов (чтение).
- В `.mcp.json` плагинов раскрываются `${VAR}` и `${VAR:-default}` из окружения (сервер с незаданной переменной пропускается с предупреждением); MCP-серверы с OAuth-входом (например, hosted context7) не подключаются: 401 в `/mcp`, обход через `${VAR}` или переопределение по имени в `mcp.servers`.
- Новая секция конфига `plugins` (`paths`, `sources`, `use_installed`, `hook_timeout_sec`; бенчи ставят `use_installed: false`); команда `gc-plugin` объявлена в дистрибутиве `gc-mcp`. Описание: `docs/plugins.md`.
- Не проверялось в ACP и `gc-server`; инструкции `!`команда`` внутри команд плагина не выполняются.

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
