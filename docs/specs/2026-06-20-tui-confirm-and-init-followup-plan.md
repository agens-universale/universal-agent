# Follow-up Plan — TUI confirm-баннер + TUI `/init` (отложенная проводка)

Дата: 2026-06-20
Статус: handoff (готов к исполнению в свежей сессии)
Ветка: `feat/layered-instructions-and-gc-protection` (от `feat/mcp-install-ux-stream` @ `4c3ff3d`)

> Этот документ — **продолжение** уже реализованных Спек A/B. Их дизайн и планы:
> - `docs/specs/2026-06-20-layered-agent-instructions-and-init-design.md` (§8/§8.1 — confirm-канал, TUI-баннер)
> - `docs/specs/2026-06-20-gc-protection-and-command-guard-design.md`
> - `docs/specs/2026-06-20-layered-agent-instructions-and-init-plan.md` (Task 6 — виджет ConfirmBanner; Task 8 — /init core)
> - Журнал исполнения и весь бэклог: `.superpowers/sdd/progress.md`

## 1. Что уже сделано (LIVE) и почему эти два пункта отложены

Реализовано и проверено ревью (23 коммита): слои инструкций L0–L3, защита `.gc` на write/read/command-дверях (фикс бага TSKBCKGRNDGNT-2 — `rm -rf .gc` детерминированно блокируется), бэкап памяти, judge `never_touch_paths`, `/init` (CLI), `--instructions-overwrite`, конфиг.

Отложены **две** интеграционные «проводки» к TUI (повышенная сложность — кросс-тред blocking; высокий риск регрессии в живом вводе TUI):

- **#2 — TUI confirm-баннер не подключён.** Виджет `ConfirmBanner` ([src/tui/widgets/confirm_banner.py](../../src/tui/widgets/confirm_banner.py)) построен (План A Task 6), но `confirm_port.request_confirm` в TUI-ветке зовёт `ctx.tui_confirm(...)`, который **никто не регистрирует**. Сейчас в TUI confirm падает на stdin `prompt_user_confirmation` (конфликтует с Textual) → фактически inert. В CLI/headless confirm работает (CLI — stdin y/N; headless — fail-closed deny). Потребители confirm: запись файлов-инструкций (always-ask) и `command_moderation` при поломке судьи (C1 Часть 2).
- **#3 — TUI `/init` — no-op.** Команда `init` зарегистрирована в реестре ([src/tui/commands.py](../../src/tui/commands.py)), но screen-handler, который реально запускает генерацию `AGENTS.md`, не реализован. В CLI `gc-cli --init` работает.

## 2. Ключевой технический контракт (паттерн `ask_human` — его и зеркалим)

Агент исполняется в **daemon-потоке** ([src/tui/services/agent_runner.py](../../src/tui/services/agent_runner.py), `_target` в отдельном `threading.Thread`). Тул `ask_human` внутри агента **блокирует** на `input()`, читающем из перенаправленного stdin, который кормит `_stdin_queue` ([stdin_bridge.py](../../src/tui/services/stdin_bridge.py)).

- TUI отдаёт ответ через `AgentRunner.submit_ask_human_reply(text)` ([agent_runner.py:149](../../src/tui/services/agent_runner.py#L149)) — кладёт строку + терминатор в `_stdin_queue`, разблокируя agent-поток.
- Main-экран ([src/tui/screens/main.py](../../src/tui/screens/main.py)) показывает `AskHumanBanner`, **прячет/блокирует поле ввода** на время вопроса, по сабмиту зовёт `submit_ask_human_reply`. Esc → clean stop треда (`_stop_for_ask_human`).

**Confirm должен повторить ровно эту механику**, но (а) для бинарного y/N, (б) триггерится **гардом** (внутри тула), а не агентом. То есть нужен **отдельный канал** (Event/Queue) для confirm, симметричный stdin-каналу ask_human, плюс thread-safe сигнал в Textual-поток (`App.call_from_thread`) на показ баннера.

**Точка инъекции ctx:** `confirm_port.request_confirm(ctx)` читает `ctx.is_tui` и `ctx.tui_confirm`. Нужно найти, какой объект приходит тулам как `self._ctx` в TUI-прогоне (строится фабрикой `RunContext` в `agent_runner`), и в TUI-режиме выставить на нём `is_tui=True` и `tui_confirm=<callable>`. В CLI/server этот объект НЕ должен иметь `is_tui=True` (там работает stdin/headless путь — не трогать).

## 3. Известный мелкий баг для попутного фикса

`ConfirmBanner.title` ([confirm_banner.py](../../src/tui/widgets/confirm_banner.py)) затеняет **реактивный** `Widget.title` Textual. Сейчас безвреден (виджет не маунтится), но при live-mount (#2) сломается. Переименовать поле в `banner_title` (в `__init__` и `compose`) + поправить тест `tests/tui/test_confirm_banner.py`.

## 4. Задачи (исполнять через subagent-driven-development; модель — sonnet)

Порядок: T0 → T1 → T2 → T3 → T4. После каждой — ревью. **Верификация — обязательно живой TUI-прогон, не только юнит-тесты** (см. §5).

### T0 — fix `ConfirmBanner.title` shadow
- Rename `title` → `banner_title` в `confirm_banner.py` + тест. `uv run pytest tests/tui/test_confirm_banner.py -v`.

### T1 — confirm-канал в `AgentRunner` (кросс-тред blocking)
- Добавить в `AgentRunner` симметрично stdin-каналу: `_confirm_queue`/`_confirm_event` + `submit_confirm_reply(ok: bool)` и метод-фабрику `make_tui_confirm() -> Callable[..., bool]`.
- `make_tui_confirm()(title, question, fields)`: из agent-потока → `App.call_from_thread(...)` показать баннер на main-экране → **блокировать** на Event → вернуть bool, когда TUI выставит ответ. Esc → False (как «нет»/cancel), без слома треда.
- Тесты: имитировать submit/cancel разблокировку (как `cancel_ask_human` тесты).

### T2 — main-экран: показать/спрятать ConfirmBanner + перехват y/N
- В `screens/main.py`: метод показа `ConfirmBanner` (sticky снизу, как AskHumanBanner), **спрятать поле ввода** на время, перехватить `y`/`n`/Enter/Esc → `runner.submit_confirm_reply(bool)` → убрать баннер, вернуть ввод.
- Переиспользовать существующую логику show/hide ввода от ask_human (не дублировать — вынести общее, если уместно).
- Стиль: класс `.confirm-banner` в `styles.tcss` (добавить, по образцу `.ask-human-banner`).
- Снапшот-тест экрана с активным confirm-баннером (`uv run pytest`).

### T3 — инъекция `is_tui` + `tui_confirm` в tool-ctx TUI-прогона
- Найти в `agent_runner` (фабрика `RunContext`/построение executor), где формируется ctx, приходящий тулам как `_ctx`. В TUI-режиме выставить `is_tui=True` и `tui_confirm=runner.make_tui_confirm()`.
- Проверить, что CLI/server путь не получает `is_tui=True` (регрессия confirm в CLI/headless недопустима).
- Тест: в TUI-режиме `request_confirm` маршрутизируется в баннер; в CLI — нет.
- **После T3 #2 закрыт:** instruction-file write ask и judge-broke ask реально показывают баннер в TUI.

### T4 — TUI `/init`-хендлер (#3)
- Хендлер команды `init` в `screens/main.py`: собрать `generate_fn` поверх TUI agent-прогона (как обычный прогон, но промптом `run_init_agents_md`), `ctx` с `is_tui=True`; вызвать `instructions.init_agents_md.run_init_agents_md(cwd, ctx, generate_fn)`; показать результат (путь/`empty`/`cancelled`) в трейсе. При существующем `AGENTS.md` confirm уже отработает через баннер (T1–T3).
- Переиспользовать `_extract_markdown` из `cli.py` (follow-up #4) для извлечения markdown из результата агента — вынести в общий модуль, если он в `cli.py`.
- Тест: юнит на сборку/маршрутизацию, насколько возможно без живого агента; остальное — ручная верификация (§5).

## 5. Верификация (обязательна — это весь смысл отдельного захода)

Юнит-тестов **недостаточно** для кросс-тред TUI. После T1–T4 прогнать **живой TUI**:
1. `uv run` TUI; задать запрос, провоцирующий запись в `AGENTS.md`/инструкцию → убедиться, что **баннер появляется снизу, ввод прячется, y/n работают, трейс виден** (не модалка).
2. Спровоцировать judge-unavailable путь (если воспроизводимо) → баннер confirm.
3. `/init` в TUI на репо без `AGENTS.md` → генерится; на репо с `AGENTS.md` → баннер overwrite/update/cancel.
4. **Регресс ask_human**: обычный `ask_human`-вопрос всё ещё работает (баннер не конфликтует с confirm-баннером); прогнать снапшот-тесты TUI.
5. CLI/headless регресс: `gc-cli --init` и confirm в CLI не сломаны.

## 6. Как стартовать свежую сессию

1. Открыть новую сессию в этом репо на ветке `feat/layered-instructions-and-gc-protection`.
2. Дать агенту этот файл как вход: «исполни `docs/specs/2026-06-20-tui-confirm-and-init-followup-plan.md` через subagent-driven-development; контекст уже сделанного — в `.superpowers/sdd/progress.md`».
3. Агент: прочитать §2 (контракт), затем T0→T4 циклом имплементер→ревью, в конце — живой TUI-прогон §5.
4. TUI-тесты — только `uv run pytest ...` (extra `[dev-tui]` в `.venv/`).

## 7. Чего тут НЕТ (вне scope этого follow-up)
- Monorepo-вложенность инструкций, импорты `@path`, подсказка про `AGENT.md` — future из Спеки A §10.
- AST-скан python-кода в command-guard (Спека B §9 known-limitation; судья — 2-я линия).
