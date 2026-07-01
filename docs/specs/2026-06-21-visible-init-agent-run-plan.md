# Visible prompt-driven /init — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Сделать `/init` (TUI) и `--init` (CLI) видимым prompt-driven прогоном агента: агент сам сканирует репозиторий и пишет `AGENTS.md` через `write_to_file`, диалог по существующему файлу — через `ask_human`; убрать невидимый фоновый прогон и всю его legacy-цепочку.

**Architecture:** Новая `build_init_prompt(cwd, can_ask)` собирает условный промпт; TUI `_cmd_init` зеркалит `_deliver_skill_with_prompt` (init-промпт как ОБЫЧНОЕ user-сообщение wire, display `/init`); CLI запускает `AgentExecutor(ctx).run([prompt])`. Старый оркестратор (`run_init_agents_md`/`resolve_init_mode`/`generate_fn`/привилегированная запись) и его инфраструктура (`agent_markdown`, `ensure_wired_ctx`→`ensure_executor`→`ctx`-property, `_show_init_result`, `gc-ua-init`-поток) удаляются.

**Tech Stack:** Python ≥3.10, pytest, Textual (TUI).

**Спека:** `docs/specs/2026-06-21-visible-init-agent-run-design.md` (§1–§8).

## Global Constraints

- Абсолютные импорты от `src/` (`from instructions.init_agents_md import ...`); без относительных и без `src.`-префикса.
- TDD: тест → провал → минимальная реализация → проход → коммит.
- TUI-тесты гонять **только** `uv run pytest ...` (extra `[dev-tui]` в `.venv/`). Прочие — `uv run pytest`.
- Коммиты лаконичные, рус/смешанный язык, **без** упоминаний AI/Claude/Anthropic и без `Co-Authored-By`.
- Init-промпт доставляется агенту как **обычное user-сообщение** (`wire_content` через `runner.start(query=...)`), **НЕ** как `technical`-блок (системный промпт трактует `<technical>` как контекст, не команды; слабые модели вправе игнорировать).
- Запись `AGENTS.md` делает **агент** (`write_to_file`) в корне проекта (в периметре → `write_guard` пропускает); привилегированный writer не используется.
- 3 пред-существующих Windows-падения (`tests/security/test_read_guard.py::test_relative_path_resolves_against_launch_root`, `::test_gc_skills_exception_allowed`, `::test_wildcard_root_allow_by_default`) — известны, игнорировать; новых не добавлять.

## File Structure

- `prompts/init_agents_md_prompt.md` — переписать: агент пишет файл сам + плейсхолдер `{existing_block}` для условной ветки.
- `src/instructions/init_agents_md.py` — оставить только `build_init_prompt(*, cwd, can_ask) -> str`; удалить `run_init_agents_md`/`resolve_init_mode`.
- `src/tui/screens/main.py` — `_cmd_init` под видимый прогон; удалить `_show_init_result` + `_work`.
- `src/cli.py` — `_run_init_agents_md` через `build_init_prompt` + `executor.run`; убрать `_extract_markdown`-импорт.
- `src/instructions/agent_markdown.py` — удалить (Task 2).
- `src/tui/app.py` / `src/tui/services/agent_runner.py` / `src/agent_executor.py` — удалить `ensure_wired_ctx`/`ensure_executor`/`ctx`-property (Task 3).

---

### Task 1: Новое ядро /init (атомарно: промпт + build_init_prompt + оба потребителя)

> Эта задача атомарна: шаблон промпта общий для старого `run_init_agents_md` и нового
> `build_init_prompt`; переписать шаблон, не сломав старый путь, нельзя — поэтому шаблон,
> новая функция, оба потребителя (TUI/CLI) и удаление старого оркестратора идут вместе.

**Files:**
- Modify: `prompts/init_agents_md_prompt.md` (переписать)
- Modify: `src/instructions/init_agents_md.py` (build_init_prompt; удалить run_init_agents_md/resolve_init_mode)
- Modify: `src/tui/screens/main.py` (_cmd_init ~2606-2655; удалить _show_init_result)
- Modify: `src/cli.py` (_run_init_agents_md ~127-153; убрать import строки 124)
- Test: `tests/instructions/test_init_agents_md.py` (переписать), `tests/tui/test_init_command.py` (переписать)

**Interfaces:**
- Produces: `build_init_prompt(*, cwd: str, can_ask: bool) -> str` (instructions/init_agents_md.py).
- Consumes: `_deliver_skill_with_prompt`-паттерн ([main.py:2306](../../src/tui/screens/main.py#L2306)); `runner.start(query, thread_id, system, message_id)`; `_track_pending(display_text, wire_content)`; `confirm_available(ctx)` (security/confirm_port.py).

- [ ] **Step 1: Переписать шаблон `prompts/init_agents_md_prompt.md`**

Полностью заменить содержимое на:

```markdown
You are setting up an AGENTS.md file that documents this codebase for future agent sessions.

Scan the repository to understand it — read the key files (README, build/config files, the main
source directories). Then WRITE the file `AGENTS.md` in the project root using the `write_to_file`
tool. You MUST actually call `write_to_file` to create the file — do not just print its content.

AGENTS.md must contain these sections: Project overview; Commands (build / test / run / lint);
Architecture (structure, key modules, entry points); Conventions (style, naming, patterns);
Important paths.

{existing_block}

After the file is written, reply with a single short line confirming the path you wrote.
Do not paste the full file content in your reply.
```

- [ ] **Step 2: Write the failing test for `build_init_prompt`**

Заменить весь файл `tests/instructions/test_init_agents_md.py` на:

```python
from instructions.init_agents_md import build_init_prompt


def test_no_file_branch_instructs_create(tmp_path):
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    assert "write_to_file" in p
    assert "create it from scratch" in p
    assert "ask_human" not in p


def test_exists_can_ask_branch_instructs_ask(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old", encoding="utf-8")
    p = build_init_prompt(cwd=str(tmp_path), can_ask=True)
    assert "ask_human" in p
    assert "UPDATE" in p and "RECREATE" in p


def test_exists_headless_branch_updates_no_ask(tmp_path):
    (tmp_path / "AGENTS.md").write_text("old", encoding="utf-8")
    p = build_init_prompt(cwd=str(tmp_path), can_ask=False)
    assert "UPDATE it in place" in p
    assert "Do NOT recreate" in p
    assert "do NOT ask" in p
    assert "ask_human" not in p
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/instructions/test_init_agents_md.py -v`
Expected: FAIL (`ImportError: cannot import name 'build_init_prompt'`)

- [ ] **Step 4: Implement `build_init_prompt` (rewrite init_agents_md.py)**

Заменить весь файл `src/instructions/init_agents_md.py` на:

```python
from __future__ import annotations

from pathlib import Path

from bundled_assets import bundled_root

_BLOCK_NEW = "There is no AGENTS.md yet — create it from scratch."
_BLOCK_ASK = (
    "An AGENTS.md already exists. First read it. Then ask the user via the ask_human tool "
    "whether to UPDATE it (refresh and augment, preserving their manual edits and structure) "
    "or RECREATE it from scratch. Act on their answer. Never silently discard the user's "
    "manual edits."
)
_BLOCK_HEADLESS = (
    "An AGENTS.md already exists. Read it and UPDATE it in place — refresh and augment, "
    "preserving manual edits and structure. Do NOT recreate from scratch, and do NOT ask."
)


def _prompt_template() -> str:
    return (bundled_root() / "prompts" / "init_agents_md_prompt.md").read_text(encoding="utf-8")


def build_init_prompt(*, cwd: str, can_ask: bool) -> str:
    """Собрать init-промпт: агент сам сканирует репо и пишет AGENTS.md через write_to_file.

    Ветка по существующему файлу зашивается здесь (вызывающая сторона передаёт can_ask
    = доступность ask_human/confirm), чтобы модель не рассуждала о доступности тулов.
    """
    exists = (Path(cwd) / "AGENTS.md").exists()
    if not exists:
        block = _BLOCK_NEW
    elif can_ask:
        block = _BLOCK_ASK
    else:
        block = _BLOCK_HEADLESS
    return _prompt_template().replace("{existing_block}", block)
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/instructions/test_init_agents_md.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Rewrite CLI `_run_init_agents_md` (cli.py)**

Заменить тело `_run_init_agents_md` (≈ строки 127-153) на:

```python
def _run_init_agents_md() -> int:
    """`gc-cli --init`: агент сам сканирует репозиторий и пишет AGENTS.md."""
    from instructions.init_agents_md import build_init_prompt
    from security.confirm_port import confirm_available

    config_dict = load_config(arg_parser.run_args.config)
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    prompt = build_init_prompt(cwd=os.getcwd(), can_ask=confirm_available(ctx))
    AgentExecutor(ctx).run([prompt])  # агент пишет AGENTS.md сам (write_to_file)
    print("[gc] --init: генерация AGENTS.md завершена", file=sys.stderr)
    return 0
```

Удалить ставшую сиротой строку импорта (≈ строка 124):
`from instructions.agent_markdown import extract_markdown as _extract_markdown`

- [ ] **Step 7: Rewrite TUI `_cmd_init` + удалить `_show_init_result` (main.py)**

Заменить `_cmd_init` и удалить `_show_init_result` (≈ строки 2606-2655) на:

```python
    def _cmd_init(self) -> None:
        """/init: видимый prompt-driven прогон — агент сам пишет AGENTS.md.

        Зеркалит _deliver_skill_with_prompt: init-промпт идёт как ОБЫЧНОЕ user-сообщение
        (wire), в трейсе — маркер "/init" (display). busy-guard отбивает активный прогон,
        поэтому здесь только два случая: auto-create треда и continue-thread."""
        runner = self.app.get_runner()
        if runner.is_running():
            self._mount_trace_row(
                prefix="[!] ", content="/init: дождитесь завершения текущей задачи",
                kind_class="line-error",
            )
            return

        import os
        from instructions.init_agents_md import build_init_prompt
        wire = build_init_prompt(cwd=os.getcwd(), can_ask=True)
        display = "/init"
        msg_id = "m_" + uuid.uuid4().hex
        self._last_user_message_id = msg_id

        if self.app.tui_state.active_task_id is None:
            tid = uuid.uuid4().hex
            self.app.tui_state.active_task_id = tid
            self._active_task_id = tid
            self._store = FileTaskStore(
                threads_dir=self.app._threads_dir or "", system=self.app._system,
            )
            runner = self.app.get_runner()
            self._prepare_for_new_run()
            self._reset_thinking_timer()
            self._on_user_text_submitted(display)
            runner.start(query=wire, thread_id=tid, system=self.app._system, message_id=msg_id)
            self.attach_active_task(tid)
            client_id = self._track_pending(display_text=display, wire_content=wire)
            self._mount_user_reply(display, state="pending", client_id=client_id)
            return

        runner = self.app.get_runner()
        client_id = self._track_pending(display_text=display, wire_content=wire)
        self._mount_user_reply(display, state="pending", client_id=client_id)
        self._prepare_for_new_run()
        self._reset_thinking_timer()
        self._on_user_text_submitted(display)
        runner.start(query=wire, thread_id=self.app.tui_state.active_task_id,
                     system=self.app._system, message_id=msg_id)
```

(Старые `_show_init_result`, `_work`, daemon-поток `gc-ua-init`, импорты `agent_markdown`/
`run_init_agents_md`/`ensure_wired_ctx` внутри `_cmd_init` — полностью удалить.)

- [ ] **Step 8: Rewrite TUI test `tests/tui/test_init_command.py`**

Заменить весь файл на (busy-guard остаётся; добавлен «зовёт runner.start с wire=init-промпт, display=/init»; тесты `_show_init_result`/`_work` удалены):

```python
"""/init: видимый prompt-driven прогон — роутинг + busy-guard."""
import pytest


class _RecRunner:
    """Стаб AgentRunner: пишет аргументы start; is_running настраивается."""
    def __init__(self, running: bool = False):
        self._running = running
        self.started: list[dict] = []
    def is_running(self) -> bool:
        return self._running
    def start(self, *, query, thread_id, system=None, message_id=None, **kw):
        self.started.append({"query": query, "thread_id": thread_id})
        return True


@pytest.mark.asyncio
async def test_cmd_init_busy_guard(tmp_path):
    """runner.is_running() → сообщение, start НЕ зовётся."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    rec = _RecRunner(running=True)
    app.active_runner = rec
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._cmd_init()
        await pilot.pause(0.1)
        assert rec.started == []
        text = "\n".join(str(s.render()) for s in scr.query("Static"))
        assert "дождитесь завершения" in text


@pytest.mark.asyncio
async def test_cmd_init_starts_run_with_init_prompt(tmp_path):
    """Не running → runner.start с init-промптом как query (wire), маркер /init в трейсе."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    rec = _RecRunner(running=False)
    # get_runner() должен вернуть наш стаб
    app.active_runner = rec
    app.get_runner = lambda: rec
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._cmd_init()
        await pilot.pause(0.1)
        assert len(rec.started) == 1
        assert "write_to_file" in rec.started[0]["query"]
        assert "AGENTS.md" in rec.started[0]["query"]
        text = "\n".join(str(s.render()) for s in scr.query("Static"))
        assert "/init" in text
```

- [ ] **Step 9: Run tests**

Run: `uv run pytest tests/instructions/test_init_agents_md.py tests/tui/test_init_command.py -v`
Expected: PASS (3 + 2)

- [ ] **Step 10: Verify no broken imports (old orchestrator gone)**

Run: `uv run pytest tests/tui -k "init" -q` и `python -c "import cli"` (через `uv run python -c "import sys; sys.argv=['x']; import cli"`).
Expected: PASS / без ImportError. Grep: `grep -rn "run_init_agents_md\|resolve_init_mode" src/` → 0.

- [ ] **Step 11: Commit**

```bash
git add prompts/init_agents_md_prompt.md src/instructions/init_agents_md.py src/cli.py src/tui/screens/main.py tests/instructions/test_init_agents_md.py tests/tui/test_init_command.py
git commit -m "feat(init): видимый prompt-driven /init — агент сам пишет AGENTS.md (TUI+CLI); удалён старый оркестратор"
```

---

### Task 2: Удалить модуль `agent_markdown` (осиротел)

**Files:**
- Delete: `src/instructions/agent_markdown.py`
- Delete: `tests/instructions/test_init_extract.py`
- Verify: `src/cli.py`, `src/tui/screens/main.py` (нет импортов agent_markdown)

**Interfaces:** ничего не Produces. После Task 1 ни один потребитель не использует `extract_markdown`.

- [ ] **Step 1: Verify orphaned**

Run: `grep -rn "agent_markdown\|extract_markdown" src/ tests/ | grep -v "\.pyc"`
Expected: совпадения только в `src/instructions/agent_markdown.py` и `tests/instructions/test_init_extract.py` (самих удаляемых файлах). Если есть другой потребитель — СТОП, сообщить (Task 1 не дочистил).

- [ ] **Step 2: Delete the module and its test**

```bash
git rm src/instructions/agent_markdown.py tests/instructions/test_init_extract.py
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/instructions tests/tui -k "init" -q`
Expected: PASS (нет ImportError).

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(init): удалить осиротевший agent_markdown (extract больше не нужен — агент пишет сам)"
```

---

### Task 3: Удалить цепочку `ensure_wired_ctx → ensure_executor → AgentExecutor.ctx`

**Files:**
- Modify: `src/tui/app.py` (удалить `ensure_wired_ctx`)
- Modify: `src/tui/services/agent_runner.py` (удалить публичный `ensure_executor`; `_ensure_executor` оставить)
- Modify: `src/agent_executor.py` (удалить `ctx`-property)
- Modify: `tests/tui/test_agent_runner.py` (удалить `test_public_ensure_executor_returns_built_once`)

**Interfaces:** ничего не Produces. После Task 1 `_cmd_init` не зовёт `ensure_wired_ctx`.

- [ ] **Step 1: Verify orphaned**

Run: `grep -rn "ensure_wired_ctx" src/ tests/ | grep -v "\.pyc"` → ожидание: только определение в `app.py` + тесты в `test_init_command.py` (которые Task 1 уже переписал — проверить, что упоминаний не осталось; если остались — СТОП).
Run: `grep -rn "\.ensure_executor()" src/ tests/ | grep -v "_ensure_executor"` → ожидание: только `app.py` (внутри `ensure_wired_ctx`, удаляемой) + `test_agent_runner.py` (удаляемый тест).

- [ ] **Step 2: Delete `ensure_wired_ctx` (app.py)**

Удалить метод `ensure_wired_ctx` целиком (≈ app.py:118-122).

- [ ] **Step 3: Delete public `ensure_executor` (agent_runner.py)**

Удалить метод `ensure_executor` (≈ agent_runner.py:51-54). Приватный `_ensure_executor` НЕ трогать (используется в `start`/`warm`).

- [ ] **Step 4: Delete `ctx`-property (agent_executor.py)**

Удалить property:
```python
    @property
    def ctx(self) -> RunContext:
        """RunContext этого запуска (read-only) — публичный шов вместо ._ctx."""
        return self._ctx
```

- [ ] **Step 5: Delete the orphaned test (test_agent_runner.py)**

Удалить функцию `test_public_ensure_executor_returns_built_once` целиком.

- [ ] **Step 6: Verify clean + run**

Run: `grep -rn "ensure_wired_ctx\|def ensure_executor\b" src/ tests/` → 0.
Run: `uv run pytest tests/tui/test_agent_runner.py tests/tui -k "init or runner" -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git commit -m "chore(tui): удалить осиротевшую цепочку ensure_wired_ctx→ensure_executor→ctx-property"
```

---

## Финальная проверка (после всех задач)

- `grep -rn "run_init_agents_md\|resolve_init_mode\|agent_markdown\|ensure_wired_ctx\|_show_init_result\|gc-ua-init" src/` → **0** (вся legacy ушла).
- `uv run pytest tests/instructions tests/tui -q` — зелено; `uv run pytest tests/security -q` — кроме 3 известных Windows read_guard.
- Живой TUI-прогон (ручной): `/init` на репо **без** `AGENTS.md` → видно скан (`read_file`) + `write_to_file`, `[running]`, спиннер; на репо **с** `AGENTS.md` → агент задаёт вопрос через ask_human-баннер «обновить/с-нуля», по ответу пишет; `/stop` отменяет.
- CLI: `gc-cli --init` (tty) → агент пишет AGENTS.md; headless → ветка «обновить, не спрашивая».
