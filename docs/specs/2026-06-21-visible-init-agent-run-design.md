# Visible prompt-driven /init (Claude-style) — Design

Дата: 2026-06-21
Статус: design (готов к плану)
Ветка: `feat/layered-instructions-and-gc-protection`
Контекст: follow-up к ранее реализованному `/init` (невидимый фоновый прогон), который
оказался неинформативным — см. §0.

## 0. Проблема

Текущий TUI `/init` ([screens/main.py::_cmd_init](../../src/tui/screens/main.py)) крутит
генерацию `AGENTS.md` в отдельном daemon-потоке `gc-ua-init` через изолированный
`AgentExecutor(ctx).run`, не подключённый к трейсу/поллингу. Итог: шапка показывает
`[stopped]`, одна статичная строка «генерирую AGENTS.md…» на 30–60с — выглядит как зависон.
Цель — сделать `/init` **видимым ходом агента** (как Claude Code): тул-вызовы, стрим,
спиннер, `[running]`, а перезапись существующего файла — диалогом самого агента.

## 1. Поведение

**TUI `/init`** — обычный видимый прогон агента:
- шапка `[running]`, `ThinkingIndicator`/строка-мысль, тул-вызовы (`read_file` по репо);
- если `AGENTS.md` уже есть — агент сам спрашивает через **ask_human-баннер** «обновить,
  сохранив правки / пересоздать с нуля», пользователь отвечает, агент пишет файл через
  `write_to_file`;
- отмена — ответить «отмена» в ask_human или `/stop` (обычный стоп прогона).

**CLI `--init`** — тот же prompt-driven прогон (агент пишет файл сам). ask доступен (tty) —
агент спрашивает через stdin; headless — промпт зашит на «обновить, не с нуля, не спрашивая».

Поведение единообразно (CLAUDE.md): один промпт-билдер + агент-пишет; различаются только
побочные эффекты видимости (TUI рендерит прогон, CLI печатает).

## 2. Init-промпт (условный, агент пишет сам)

`prompts/init_agents_md_prompt.md` переписывается: вместо «выдай markdown» →
«просканируй репозиторий и **запиши `AGENTS.md` сам** через `write_to_file` в корень проекта».
Ветка по существующему файлу зашивается **вызывающей стороной** (она знает доступность ask
через `confirm_available(ctx)`), чтобы модель не рассуждала о доступности тулов:

- **файла нет** → «создай `AGENTS.md`».
- **есть + ask доступен** → «прочитай существующий `AGENTS.md`; спроси пользователя через
  `ask_human`: обновить (сохранив ручные правки) или пересоздать с нуля; действуй по ответу».
- **есть + ask недоступен (headless)** → «прочитай и **обнови**, сохранив ручные правки;
  **не** пересоздавай с нуля; не спрашивай».

Безопасный дефолт во всех ветках: ручные правки молча не удаляются. Промпт усилен явным
«ты ОБЯЗАН записать `AGENTS.md` через `write_to_file`» (надёжность на слабых моделях).

## 3. Архитектура

### 3.1. `instructions/init_agents_md.py` — ужать до сборки промпта
Новая единственная публичная функция:
```
def build_init_prompt(*, cwd: str, can_ask: bool) -> str
```
- читает шаблон `prompts/init_agents_md_prompt.md` (через `bundled_root()`);
- определяет существование `AGENTS.md` в `cwd`;
- подставляет ветку §2 (нет файла / есть+can_ask / есть+headless) и путь к файлу;
- НЕ запускает агента, НЕ пишет файл, НЕ зовёт confirm.

Уходят: `resolve_init_mode`, `run_init_agents_md`, `generate_fn`-контракт, привилегированный
`target.write_text`, импорт `security.confirm_port`.

### 3.2. TUI — `screens/main.py::_cmd_init`
Вместо daemon-потока + `AgentExecutor(ctx).run`:
- busy-guard остаётся (`runner.is_running()` → «дождитесь завершения»);
- `prompt = build_init_prompt(cwd=os.getcwd(), can_ask=True)` (TUI всегда интерактивен);
- **зеркалить `_deliver_skill_with_prompt`** ([main.py:2306](../../src/tui/screens/main.py#L2306)) —
  это уже готовый шов «display ≠ wire»: он обрабатывает все 3 случая (run активен → `add_message`;
  нет задачи → auto-create тред + `runner.start` + `attach`; задача есть, run не идёт → continue-thread),
  трекает pending (`_track_pending(display_text=…, wire_content=…)`) и зовёт `runner.start(query=wire)`.
  Для `/init`: `wire_content = init_prompt`, `display_text = "/init"` (маркер в трейсе);
- **init-промпт доставляется как ОБЫЧНОЕ user-сообщение, НЕ как `technical`-блок.** Системный
  промпт трактует `<technical>` как *контекст, не команды*, и слабые модели (MiniMax) вправе их
  игнорировать (см. docstring `_deliver_skill_with_prompt` и историю inline-/skill). Это load-bearing
  для надёжности записи файла;
- дальше всё бесплатно от обычного пути: трейс, jsonl-поллинг (`wire`-реконсиляция), `ThinkingIndicator`,
  `[running]`, создание треда если `/init` — первое действие.

Уходят: `_show_init_result`, `_work`/`generate_fn`/`gc-ua-init`-поток, вызов `ensure_wired_ctx`.

### 3.3. CLI — `cli.py::_run_init_agents_md`
- `ctx = create_run_context(...)`;
- `prompt = build_init_prompt(cwd=os.getcwd(), can_ask=confirm_available(ctx))`;
- `AgentExecutor(ctx).run([prompt])` — агент пишет файл сам;
- уходят: `generate_fn`-замыкание, `_extract_markdown`/extract, `confirm_ctx`-SimpleNamespace.

### 3.4. Запись файла — через агента
`AGENTS.md` создаётся агентом (`write_to_file`) в корне проекта (= `launch_root`, в периметре)
→ `write_guard` пропускает без вопроса; `protected_paths` его не покрывает. Привилегированный
writer больше не нужен.

## 4. Edge-cases

- **Отмена:** ask_human «отмена» → агент завершает без записи; либо `/stop`.
- **Агент не записал файл** (слабая модель проигнорировала `write_to_file`): видно в трейсе
  (прогон прошёл, файла нет) — не молчаливый провал. Детерминированного фолбэка-записи нет
  (осознанный размен на prompt-driven C-agent).
- **headless CLI:** ask недоступен → ветка «обновить, не спрашивая».
- **`/init` во время активного прогона:** busy-guard — «дождитесь завершения».

## 5. Тестирование

- `build_init_prompt`: 3 ветки (нет файла / есть+can_ask / есть+headless) дают правильную
  инструкцию (юнит на содержимое).
- TUI: `_cmd_init` зовёт `runner.start` с init-промптом как wire + маркер `/init` в трейсе;
  busy-guard работает; не плодит `gc-ua-init`-поток (юнит/`run_test`).
- CLI: `_run_init_agents_md` строит промпт по `confirm_available(ctx)` и запускает executor
  (юнит с моками executor/ctx).
- Живой TUI-прогон (ручной): `/init` без файла → виден скан+запись; с файлом → ask_human-баннер
  «обновить/с-нуля» → запись; `/stop` отменяет.

## 6. Вне scope

Авто-merge-стратегии (diff/3-way), флаг `/init fresh`, кастомный путь/имя файла. Память правил
confirm (только что сделанная) не задевается.

## 7. Удаление/чистка (всё — наследие старого `/init`)

Проверено грепом: каждый символ ниже трассируется только к старому `/init`.

**Цепочка «невидимого прогона» (осиротеет целиком):**
- `TuiApp.ensure_wired_ctx()` ([app.py:118](../../src/tui/app.py#L118)) — потребитель только
  `_cmd_init` → удалить.
- `AgentRunner.ensure_executor()` (публичная, [agent_runner.py:51](../../src/tui/services/agent_runner.py#L51))
  — потребитель только `ensure_wired_ctx` → удалить (приватный `_ensure_executor` остаётся —
  он в `start`/`warm`).
- `AgentExecutor.ctx`-property — единственный потребитель `ensure_wired_ctx` (везде в коде
  `context.ctx` — это **другой** объект, ToolSecurityContext) → удалить.

**Логика старого init:**
- `run_init_agents_md`, `resolve_init_mode` → заменяются на `build_init_prompt`; `generate_fn`,
  привилегированный `target.write_text`, импорт `confirm_port` → удалить.
- `instructions/agent_markdown.py` (весь модуль: `extract_markdown`/`_strip_code_fence`/
  `_AGENT_ERROR_PREFIXES`) + re-export в `cli.py` — потребители только оба `/init` → удалить.
- `TraceScreen._show_init_result`, daemon `gc-ua-init`, `_work`/`generate_fn` в `_cmd_init` → удалить.

**Осиротевшие тесты:**
- `tests/instructions/test_init_extract.py` → удалить.
- `tests/instructions/test_init_agents_md.py` → переписать под `build_init_prompt` (3 ветки).
- `tests/tui/test_init_command.py` → переписать: busy-guard остаётся; `_show_init_result`-тесты
  и `_work`-exception-тест уходят; добавить «`_cmd_init` зовёт `runner.start` с init-промптом».
- `tests/tui/test_agent_runner.py::test_public_ensure_executor_returns_built_once` → удалить.

**Верификация чистоты:** после удаления — греп на каждое имя (0 потребителей) + полный прогон
`tests/instructions tests/tui` + security зелёный (кроме 3 известных пред-сущ. Windows read_guard).

## 8. Затрагиваемые файлы (ориентир для плана)

- `prompts/init_agents_md_prompt.md` — переписать (агент-пишет + условная ask/update-ветка).
- `src/instructions/init_agents_md.py` — `build_init_prompt`; удалить `run_init_agents_md`/`resolve_init_mode`.
- `src/instructions/agent_markdown.py` — удалить (модуль).
- `src/cli.py` — `_run_init_agents_md` под `build_init_prompt`+executor; убрать `_extract_markdown` re-export.
- `src/tui/screens/main.py` — `_cmd_init` под `runner.start`+wire-маркер; удалить `_show_init_result`.
- `src/tui/app.py` — удалить `ensure_wired_ctx`.
- `src/tui/services/agent_runner.py` — удалить публичный `ensure_executor`.
- `src/agent_executor.py` — удалить `ctx`-property.
- тесты — см. §7.
