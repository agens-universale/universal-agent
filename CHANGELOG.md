# Changelog

## Unreleased

### Breaking — рецепты MCP: `install.source` стал именем каталога (0.4.0 → 0.5.0)

- **`install.source` в манифесте MCP теперь ИМЯ каталога, а не относительный путь.**
  Было `source: ../external/office-mcp`, стало `source: office-mcp`. Кастомные
  манифесты (`--config`, `<gc_config>/recommended_mcp/config.local.yml`) требуют
  правки: уберите ведущие `../` и любые слэши, оставьте одно имя.
- Причина — переезд рецептов в отдельный дистрибутив `gc-mcp`
  (`packages/mcp/src/recommended_mcp/`). Путь резолвился ОТНОСИТЕЛЬНО каталога
  рецептов и работал, пока рецепты и `external/` были соседями и в репозитории,
  и в колесе. После переезда глубины разошлись: в колесе соседство держит
  `force-include`, а в рабочей копии тот же путь стал вести в
  `packages/mcp/src/external/…`. Локальная установка при этом не падала, а
  **молча съезжала в git-метод** — для офлайн-контура это отказ.
- Арифметика путей убрана совсем: имя ищет `install._vendor_candidates` по
  известным местам (рядом с рецептами — в колесе; в `external/vendor/` корня
  репозитория — в рабочей копии), а `_repo_subdir` строит из того же имени
  git-путь `#subdirectory=external/vendor/<имя>`. Следующий переезд это
  переживёт.
- Совместимость: старый формат **терпится**, но контрактом больше не является.
  Из `../external/office-mcp` берётся последний сегмент (`office-mcp`), и один
  раз на процесс печатается `[warn] install.source … — устаревший формат (путь)`;
  установка идёт до конца. Отвергается только вырожденное значение, из которого
  имени не выделить (`""`, `.`, `..`, голый слэш): такой рецепт пропускается
  строкой `[warn] <имя>: пропущен — …`, остальные ставятся, код возврата 0.
  Терпимость переходная — правьте манифест.
- Заодно: исходники вендоренных MCP переехали из `external/<имя>-mcp/` в
  `external/vendor/<имя>-mcp/`. Это тот же переезд, и на него завязан git-путь
  выше.

### Исправлено — `gc_server.cleanup_threads`: дефолт `--threads-dir` больше не от CWD

- **Дефолт `--threads-dir` берётся оттуда же, откуда его берёт фоновый janitor
  сервера** — из серверного конфига через `resolve_threads_dir`. До этого
  подставлялось голое `"threads"` относительно CWD: в cron с чужим рабочим
  каталогом команда печатала «threads_dir not found» и возвращала 0, то есть
  тихо не делала ничего, и треды копились месяцами.
- Причина: пока чистка лежала в `scripts/`, дефолт считался якорем от файла
  скрипта (`<script>/../threads`) и от CWD не зависел. Переезд внутрь пакета
  (`28fdc67c`) этот якорь обессмыслил, а замена оказалась привязана к CWD.
  Прежний якорь не восстанавливаем: каталога `<репо>/threads` не существовало
  никогда, ни в дереве, ни в истории — он был рудиментом.
- От CWD дефолт по-прежнему зависит, и это не дефект, а модель продукта:
  `project_slug()` даёт каждому рабочему каталогу свой стор тредов, как у CLI и
  TUI. Ставьте cron с CWD проекта либо передавайте `--threads-dir` явно.
- Выбранный путь всегда печатается абсолютным, в том числе в статистике: по
  строке `"threads"` нельзя было понять, какой каталог смотрели.
- Совместимость не нарушена: флаг остаётся необязательным, коды возврата
  прежние.

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
