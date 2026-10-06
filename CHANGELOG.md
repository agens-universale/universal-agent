# Changelog

## Unreleased

### Breaking — типизированные настройки вместо плоского `config_dict`

- Конфиг загружается в неизменяемые pydantic-модели: `gc_core.config.load_settings(path, profile_override=...)` → `LoadedSettings(settings: AppConfig, runtime: RuntimeEnv, provenance, profile_meta)`. Плоского словаря больше нет: удалены `load_config`, `load_config_ex`, `flatten_app_config`, `flat_config` и поле `RunContext.config_dict`.
- SDK: `AsyncAgent.create(settings)` и `acreate_run_context(settings)` принимают `LoadedSettings`/`AppConfig`; словарь — `TypeError`. Правка — новым объектом: `loaded.with_overrides({"agent.max_steps": 5})`, `override_settings(app_cfg, {...})`. Чтение — `ctx.settings` / `ctx.runtime` (`gc_core.settings.settings_of`/`runtime_of`).
- `gc-cli config` и `gc-ua config`: `explain` (значение и источник каждой настройки — файл, профиль, managed, `${ENV}`, CLI; секреты замаскированы), `validate` (проверка без запуска агента), `schema` (JSON Schema).
- `${ENV:VAR}`/`${FILE:path}` строгие: незаданный источник — ошибка при старте с файлом и ключом; необязательное значение — `${ENV:VAR:-умолчание}`. Отказ — только если значение дошло до итогового конфига: ключ, перекрытый проектом или профилем, загрузку не роняет. Исключение — правило пары адрес+токен записи реестра: оно проверяется по каждому документу, и незаданная переменная в половине пары остаётся ошибкой. `gc-cli --update` работает и со сломанным конфигом.
- Плоские имена прежнего `config_dict` удалены отовсюду: `--param` (`--max_steps 50` → `--agent.max_steps 50`), `agent_overrides` сервера (webhook и swarm), `gc config explain`, профили бенча (`agent_kwargs.gc_cli_config`) и тестовые хелперы принимают только путь настройки через точку (или вложенную секцию — в `agent_overrides` и профилях бенча). Неизвестное имя — ошибка; если у схемы есть настройка с тем же последним сегментом, сообщение подсказывает путь: `неизвестная настройка --max_steps (возможно, --agent.max_steps)`. Объявленные флаги CLI (`--model`, `--completions_url`, `--profile`, `--level`, `--write-guard`, `--trace-id`…) остаются флагами.
- Правки из CLI (`--param`), `agent_overrides` сервера и сессионные правки TUI **заменяют** значение по своему пути, как прежний `config_dict.update`: `--mcp.servers '{}'` отключает серверы, список `allowed_read_roots` сужается, `--models` заменяет запись вместе с адресом и токеном. Секция-словарь раскладывается на поля (не сбрасывает остальные поля секции), адрес записи реестра без токена — ошибка правила пары; swarm ведёт себя так же. Флаги CLI не сокращаются (`allow_abbrev=False`). Объединение списков — только у файловых слоёв.
- Негодное значение `--param`/`agent_overrides` — сообщение с ключом и причиной без самого значения (`hide_input_in_errors`): токен не печатается в трейсбеке. Секреты внутри словарей без схемы (`*_kwargs`, `extra_body`) маскируются по имени ключа в TUI `/config` и `gc config explain`. Невалидное сессионное значение в мете треда снимается при открытии с уведомлением.
- Лишний ключ в YAML — уведомление (файл, ключ, подсказка «возможно, …») вместо отказа загрузки; в `--param` — по-прежнему ошибка. `config_version` и реестр миграций; межполевые проверки.
- Метаданные полей (`secret`, `worker_only`, `expert`, `restart`) — рядом с полями модели; токены — `SecretStr`, `repr` настроек секретов не показывает.
- `models.<alias>.backend` больше не принимает `api_mode`, `stream`, `send_tool_strict`, `openai_strict_tools_schema` — на уровне модели они никогда не применялись (теперь уведомление).
- Гвард команд защищает `security.protected_paths.extra`; каталоги `security.write_guard.extra_paths` (разрешённые для записи) идут обычной модерацией команд.
- `logging.propagate` из YAML теперь действует. TUI `/config` показывает настройки документом. Перечитка конфига в TUI атомарна.
- Тестам: `gc_testkit.settings.make_settings` / `fake_ctx` / `update_ctx`, `gc_testkit.helpers.tool_ctx.tool_settings`, `gc_testkit.tui_support.tui_settings` — ключи только путями через точку (`{"agent.max_steps": 3}`) или секциями; `settings_from_flat` удалён.

### Feature — нативный Temporal backend

- Добавлен опциональный пакет `gc-ua-temporalio`: штатные `AgentExecutor` и `ThreadSession` работают через workflow, а обращения к модели, внешние эффекты инструментов и операции хранилища выполняются в activities. Локальное исполнение остаётся режимом по умолчанию.
- Добавлены native worker/client, backend `temporal-native` для server и remote CLI/TUI, подтверждения, stop, дочерние workflow и управление persistent MCP-ресурсами.
- FileThreadStore и SQLite поддерживают квитанции операций и восстановление записи; неопределённый исход внешнего эффекта требует явного операторского решения. Continue-As-New сохраняет очередь доставки, включая большие checkpoint.
- Конфигурация, архитектура и ограничения текущего deployment описаны в [документации Temporal](docs/temporal-configuration.md). Legacy workflow сохранён отдельно; автоматическая миграция его истории не выполняется.
- Профиль native-воркера без `revision`: workflow знает профиль по имени, настройки и токены берутся из текущего процесса воркера. Смена конфигурации или ротация токена — перезапуск воркера; живые сессии продолжают работу. Ключ `revision` в JSON воркера и флаг `--revision` клиента удалены.
- `payload_storage` включён по умолчанию (каталог `payloads` рядом с `receipts_database`); `null` выключает. `prune_after_days` — очистка blob'ов воркером.
- **Сменился путь тредов по умолчанию**: без `thread_stores` воркер берёт каталог `threads` рядом с `receipts_database`, а не `threads` от cwd (workspace агента). Установка, полагавшаяся на умолчание, откроет пустые треды — задайте прежний путь явно (`"thread_stores": {"files": {"kind": "file", "path": "<workspace>/threads"}}`) или перенесите каталог.
- `gc-temporal-native unlock` снимает владение тредом у сессии, которой больше нет.
- Секция `activities` конфига воркера: таймауты и повторы activities по уровням (`default`, `model`, `resource`, `thread_store`, `tools.<имя>`), как `ActivityConfig` у Pydantic AI. Сбой провайдера модели (обрыв, 5xx, 408/425/429) теперь повторяет Temporal — по умолчанию без ограничения, с паузой до минуты: ход ждёт провайдера, а не завершается FAILED. Эффекты по-прежнему исполняются не более одного раза. `NativeAgentClient.start` принимает `retry_policy` и `execution_timeout` для всего workflow (по умолчанию не заданы).
- В ядре — базовый класс доменного отказа хода `TurnFailure` (`ExecutionFailure`, ошибка провайдера, `ContextWindowExceeded`): durable-исполнитель переводит его в FAILED, прочие исключения кода workflow — в повтор workflow task.
- Сессия снимает владение тредом при закрытии после Continue-As-New и при падении; сбой коммита в режиме `persist: turn` не теряет шаги хода; сбой закрытия MCP не подменяет готовый ответ.

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
