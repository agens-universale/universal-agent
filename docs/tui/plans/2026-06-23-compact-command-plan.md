# Команда /compact — принудительное сжатие сессии — Implementation Plan (План 2 из 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Команда `/compact` (алиас `/сжать`) — сжать контекст сессии сейчас, не дожидаясь порога, с антиспам-защитой по контенту.

**Architecture:** Форс-сжатие = та же `run_summary_compaction`, что и автосжатие, но вызванная вручную и без порога. Новый вход `AgentExecutor.compact_thread(thread_id, system)` загружает тред (как старт `run()`), строит state + `RunSummaryCoordinator`, зовёт новый `force_compact_once()`. `AgentRunner.compact_now()` гоняет это off-thread; TUI `_cmd_compact` проверяет гарды и запускает.

**Tech Stack:** Python ≥3.10, Textual, pytest.

> **Mechanism note (supersedes spec Component 2):** spec предполагал `compact_now` на живом `state`, но в простое живого state нет → форс-сжатие ЗАГРУЖАЕТ тред (см. `compact_thread`). Run-loop НЕ трогаем (путь автосжатия в `run()` остаётся как есть).

## Global Constraints

- Абсолютные импорты от `src/`. TUI-тесты — `uv run pytest`; не-TUI — `pytest`.
- Команда `compact`, алиас `сжать`. Сжатие переиспользует существующий `run_summary_compaction` (домен суммаризации НЕ дублируем, run-loop НЕ трогаем).
- Гарды `_cmd_compact` (порядок): (1) только `backend_api_mode == chat_completions`; (2) только в простое (нет активного прогона / ask_human); (3) антиспам по контенту: отказ если `context_chars < 0.20 * threshold`. Метрики — теми же `parse_summary_trigger_chars` + `calc_history_chars_for_trigger(read_live_messages)`, что и индикатор.
- При срабатывании — `tui_summarizing` (плашка «сжимаю контекст…») взводится автоматически (`_summarizing_flag` внутри координатора).
- При правках суммаризации прогонять `test_summary_*`, `test_thread_summarizer*`.

## File Structure

- Modify `src/run_summary_coordinator.py` — `RunSummaryCoordinator.force_compact_once()`.
- Modify `src/agent_executor.py` — `AgentExecutor.compact_thread(thread_id, system)`.
- Modify `src/tui/services/agent_runner.py` — `AgentRunner.compact_now()`.
- Modify `src/tui/screens/main.py` — `_cmd_compact()` + диспетч команды `compact`.
- Modify `src/tui/commands.py` — регистрация `compact` (+алиас `сжать`).
- Tests: `tests/test_force_compact_once.py`, `tests/test_compact_thread.py`, `tests/tui/test_compact_now.py`, `tests/tui/test_cmd_compact.py`, `tests/tui/test_commands.py` (+).

---

## Task 1: `RunSummaryCoordinator.force_compact_once`

**Files:**
- Modify: `src/run_summary_coordinator.py`
- Test: `tests/test_force_compact_once.py`

**Interfaces:**
- Produces: `force_compact_once(self, *, trigger_reason: str = "manual") -> None` — один безусловный проход `run_summary_compaction` (без проверки порога), с `_summarizing_flag` и `on_summary_applied`. Для `backend != chat_completions` — no-op.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_force_compact_once.py`:

```python
"""force_compact_once: один безусловный проход сжатия (без проверки порога)."""
import types

from run_summary_coordinator import RunSummaryCoordinator


def _coord(monkeypatch, calls, backend="chat_completions"):
    state = types.SimpleNamespace(backend_api_mode=backend)
    coord = RunSummaryCoordinator(
        ctx=types.SimpleNamespace(), state=state, thread_session=None,
        summary_trigger_chars=10_000, is_cli_mode=True,
        pre_backend_trigger_reason="pre_backend", logger=None,
    )
    monkeypatch.setattr(
        "run_summary_coordinator.run_summary_compaction",
        lambda **kw: calls.append(kw),
    )
    return coord


def test_force_compacts_once_ignoring_threshold(monkeypatch):
    calls = []
    coord = _coord(monkeypatch, calls)
    coord.force_compact_once()
    assert len(calls) == 1
    assert calls[0]["trigger_reason"] == "manual"
    # порог НЕ проверяется — сжали несмотря на пустую/малую историю
    assert calls[0]["trigger_threshold_chars"] == 10_000


def test_force_noop_in_responses_mode(monkeypatch):
    calls = []
    coord = _coord(monkeypatch, calls, backend="responses")
    coord.force_compact_once()
    assert calls == []
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/test_force_compact_once.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'force_compact_once'`.

- [ ] **Step 3: Реализовать метод**

В [src/run_summary_coordinator.py](../../../src/run_summary_coordinator.py) в классе `RunSummaryCoordinator`, после `pre_run_compact_if_needed`, добавить:

```python
    def force_compact_once(self, *, trigger_reason: str = "manual") -> None:
        """Один безусловный проход сжатия (ручной /compact) — БЕЗ проверки порога.
        Та же машинерия, что и автосжатие; персист через on_summary_applied."""
        state = self._state
        if state.backend_api_mode != "chat_completions":
            return
        with _summarizing_flag(self._thread_session):
            run_summary_compaction(
                ctx=self._ctx,
                state=state,
                trigger_reason=trigger_reason,
                is_cli_mode=self._is_cli_mode,
                on_summary_applied=self.on_summary_applied,
                trigger_threshold_chars=self._summary_trigger_chars,
            )
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/test_force_compact_once.py -q`
Expected: PASS (2).

- [ ] **Step 5: Commit**

```bash
git add src/run_summary_coordinator.py tests/test_force_compact_once.py
git commit -m "summary: force_compact_once — безусловный проход сжатия для /compact"
```

---

## Task 2: `AgentExecutor.compact_thread`

**Files:**
- Modify: `src/agent_executor.py`
- Test: `tests/test_compact_thread.py`

**Interfaces:**
- Consumes: `force_compact_once` (T1); существующие `ThreadSession.from_config`, `repair_tool_call_pairing`, `parse_summary_trigger_chars`, `ThreadMetaMapper.shared_from_meta`, `State`.
- Produces: `AgentExecutor.compact_thread(self, thread_id: str, system: str | None = None) -> bool` — загружает тред, строит минимальный state + координатор, зовёт `force_compact_once`. True если сжатие выполнено (chat-режим + тред есть), иначе False.

**Контекст:** зеркалит загрузочную часть `run()` ([agent_executor.py:90-258](../../../src/agent_executor.py#L90)) в усечённом виде — ТОЛЬКО то, что нужно сжатию (history + intent/shared + coordinator), без tools-loop/промпт-турна. Run-loop не трогаем.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/test_compact_thread.py`:

```python
"""compact_thread: грузит тред + форс-сжатие через координатор."""
import types

import agent_executor as ae


def test_compact_thread_calls_force_compact(monkeypatch, tmp_path):
    captured = {}

    class _TS:
        history_messages = [{"role": "user", "content": "привет", "id": "u1"}]
        meta = {}
        thread_id = "t1"
        system_id = "cli"
        threads_dir = str(tmp_path)

    class _Coord:
        def __init__(self, **kw):
            captured["coord_state"] = kw["state"]
        def force_compact_once(self, **kw):
            captured["forced"] = True
        def on_summary_applied(self, ev):
            pass

    class _FakeState:
        # Мокаем State, чтобы НЕ дёргать реальный конструктор (он зовёт
        # get_project_data → WMI-хэнг на Windows). Capture messages.
        def __init__(self, **kw):
            self.messages = []
            self.backend_api_mode = "responses"
            self.shared = kw.get("shared")
            self.thread_id = kw.get("thread_id")
            self.dialog_steps_offset = 0
        def init_user_intent_log(self, v): pass
        def init_current_user_intent(self, v): pass
        def init_user_turn_seq(self, v): pass
        def infer_user_turn_seq_if_missing(self): pass

    monkeypatch.setattr(ae, "ThreadSession", types.SimpleNamespace(from_config=lambda **kw: _TS()))
    monkeypatch.setattr(ae, "repair_tool_call_pairing", lambda m, **kw: m)
    monkeypatch.setattr(ae, "RunSummaryCoordinator", _Coord)
    monkeypatch.setattr(ae, "parse_summary_trigger_chars", lambda c: 5000)
    monkeypatch.setattr(ae, "State", _FakeState)

    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "chat_completions"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    ex._state = None
    ex._stop_pending = False

    ok = ex.compact_thread("t1", system="cli")
    assert ok is True
    assert captured.get("forced") is True
    # история треда загружена в state, по которому идёт сжатие
    assert any(m.get("content") == "привет" for m in captured["coord_state"].messages)


def test_compact_thread_noop_in_responses(monkeypatch, tmp_path):
    ex = ae.AgentExecutor.__new__(ae.AgentExecutor)
    ex._ctx = types.SimpleNamespace(
        config_dict={"model": "M", "max_steps": 30, "system_prompt_id": "x",
                     "backend_api_mode": "responses"},
        tools_spec=[], tools_description=[],
    )
    ex._lock = __import__("threading").Lock()
    assert ex.compact_thread("t1", system="cli") is False
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `pytest tests/test_compact_thread.py -q`
Expected: FAIL — `AttributeError: 'AgentExecutor' object has no attribute 'compact_thread'`.

- [ ] **Step 3: Реализовать `compact_thread`**

В [src/agent_executor.py](../../../src/agent_executor.py) в классе `AgentExecutor` добавить метод (импорты `ThreadSession`, `repair_tool_call_pairing`, `RunSummaryCoordinator`, `parse_summary_trigger_chars`, `ThreadMetaMapper`, `State` уже есть в модуле — используются в `run()`):

```python
    def compact_thread(self, thread_id: str, system: str | None = None) -> bool:
        """Принудительное сжатие треда (ручной /compact) — вне run-loop.

        Грузит тред, строит минимальный state (только нужное сжатию) + координатор,
        зовёт force_compact_once. True если сжатие выполнено (chat-режим + есть тред)."""
        config_dict = self._ctx.config_dict
        backend_api_mode = str(config_dict.get("backend_api_mode", "responses")).strip().lower()
        if backend_api_mode != "chat_completions" or not thread_id:
            return False
        system_id = system or "cli"
        thread_session = ThreadSession.from_config(
            config_dict=config_dict, system_id=system_id, thread_id=thread_id,
        )
        thread_session.history_messages = repair_tool_call_pairing(
            thread_session.history_messages, keep_latest_unanswered=False,
        )
        meta = getattr(thread_session, "meta", None) or {}
        thread_shared = ThreadMetaMapper.shared_from_meta(meta)

        state = State(
            verbose=bool(config_dict.get("verbose", False)),
            model=config_dict["model"],
            tools_spec=self._ctx.tools_spec,
            tools_descr=self._ctx.tools_description,
            skills_list=[],
            max_steps=int(config_dict.get("max_steps", 30) or 30),
            system_prompt_id=config_dict.get("system_prompt_id", ""),
            use_server_subagents=bool(config_dict.get("use_server_subagents", False)),
            thread_id=thread_id,
            shared=thread_shared,
            workspace_config=config_dict,
        )
        state.backend_api_mode = "chat_completions"
        state.dialog_steps_offset = int(meta.get("dialog_steps_offset", 0) or 0)
        state.init_user_intent_log(meta.get("user_intent_log"))
        state.init_current_user_intent(meta.get("current_user_intent"))
        state.init_user_turn_seq(meta.get("user_turn_seq"))
        state.infer_user_turn_seq_if_missing()
        state.messages = [
            _shallow_copy_msg(m)
            for m in thread_session.history_messages
            if isinstance(m, dict)
        ]

        coord = RunSummaryCoordinator(
            ctx=self._ctx, state=state, thread_session=thread_session,
            summary_trigger_chars=parse_summary_trigger_chars(config_dict),
            is_cli_mode=(system_id == "cli"),
            pre_backend_trigger_reason="pre_backend", logger=getattr(self._ctx, "logger", None),
        )
        setattr(self._ctx, "on_summary_applied", coord.on_summary_applied)
        coord.force_compact_once(trigger_reason="manual")
        return True
```

> `_shallow_copy_msg` уже определён в модуле (используется в `run()`). Если в тесте мокается `State` целиком — не требуется; на проде State-конструктор реальный.

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `pytest tests/test_compact_thread.py -q`
Expected: PASS (2).

- [ ] **Step 5: Прогнать суммаризацию (хрупкое)**

Run: `pytest tests/test_summary_pipeline.py tests/test_compact_thread.py -q`
Expected: PASS (без новых падений).

- [ ] **Step 6: Commit**

```bash
git add src/agent_executor.py tests/test_compact_thread.py
git commit -m "executor: compact_thread — форс-сжатие треда вне run-loop"
```

---

## Task 3: `AgentRunner.compact_now`

**Files:**
- Modify: `src/tui/services/agent_runner.py`
- Test: `tests/tui/test_compact_now.py`

**Interfaces:**
- Consumes: `AgentExecutor.compact_thread` (T2); `self._ensure_executor()`, `self._on_error`.
- Produces: `AgentRunner.compact_now(self, thread_id: str, system: str) -> bool` — off-thread `executor.compact_thread`. False если worker занят; True если поток запущен. Ошибка → `_on_error`.

- [ ] **Step 1: Написать падающий тест**

Создать `tests/tui/test_compact_now.py`:

```python
"""compact_now: off-thread форс-сжатие, отказ если worker занят."""
import time

from tui.services.agent_runner import AgentRunner


def test_compact_now_runs_off_thread():
    seen = {}

    class _Ex:
        def compact_thread(self, tid, system=None):
            seen["args"] = (tid, system)
            return True

    # AgentRunner(executor=...) — executor задаётся напрямую (см. ctor: executor /
    # executor_factory / on_error). _ensure_executor вернёт этот executor.
    runner = AgentRunner(executor=_Ex())
    assert runner.compact_now("t1", "cli") is True
    for _ in range(100):
        if "args" in seen:
            break
        time.sleep(0.01)
    assert seen["args"] == ("t1", "cli")


def test_compact_now_rejects_when_busy():
    import threading

    class _Ex:
        def compact_thread(self, tid, system=None):
            return True

    runner = AgentRunner(executor=_Ex())
    runner._thread = threading.Thread(target=lambda: time.sleep(0.5))
    runner._thread.start()
    try:
        assert runner.compact_now("t1", "cli") is False
    finally:
        runner._thread.join()
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_compact_now.py -q`
Expected: FAIL — `AttributeError: ... 'compact_now'`.

- [ ] **Step 3: Реализовать `compact_now`**

В [src/tui/services/agent_runner.py](../../../src/tui/services/agent_runner.py) добавить (рядом с `start`):

```python
    def compact_now(self, thread_id: str, system: str) -> bool:
        """Off-thread форс-сжатие треда. False если worker занят."""
        import threading
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False

            def _target() -> None:
                try:
                    executor = self._ensure_executor()
                    executor.compact_thread(thread_id, system=system)
                except Exception as exc:
                    if self._on_error is not None:
                        try:
                            self._on_error(exc)
                        except Exception:
                            pass

            t = threading.Thread(target=_target, name="gc-ua-compact", daemon=True)
            self._thread = t
        t.start()
        return True
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_compact_now.py -q`
Expected: PASS (2).

- [ ] **Step 5: Commit**

```bash
git add src/tui/services/agent_runner.py tests/tui/test_compact_now.py
git commit -m "tui(runner): compact_now — off-thread форс-сжатие"
```

---

## Task 4: TUI `_cmd_compact` + гарды

**Files:**
- Modify: `src/tui/screens/main.py` (`_cmd_compact` + диспетч в обработчике команд)
- Test: `tests/tui/test_cmd_compact.py`

**Interfaces:**
- Consumes: `runner.compact_now` (T3); `parse_summary_trigger_chars`, `calc_history_chars_for_trigger`, `store.read_live_messages`; `_task_status_badge`.
- Produces: `_cmd_compact(self) -> None` — гарды + запуск; диспетч `name == "compact"` → `_cmd_compact()`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/tui/test_cmd_compact.py`:

```python
"""_cmd_compact: гарды (режим/простой/контент) + запуск compact_now."""
import json
import pytest


def _write_task(tmp_path, tid, content_chars=0):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8")
    body = "x" * content_chars
    line = json.dumps({"type": "message", "thread_id": tid, "system": "cli", "seq": 1,
                       "ts": "t", "payload": {"role": "user", "content": body}})
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(line + "\n", encoding="utf-8")


class _Runner:
    def __init__(self, running=False):
        self._running = running
        self.compacted = False
    def is_running(self):
        return self._running
    def compact_now(self, tid, system):
        self.compacted = True
        return True


def _chat_cfg():
    return {"model": "M", "backend_api_mode": "chat_completions",
            "summary_params": {"chat_completions_summary_trigger_chars": 1000}}


@pytest.mark.asyncio
async def test_compact_runs_when_idle_and_enough_content(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000001a"
    _write_task(tmp_path, tid, content_chars=500)   # 50% порога ≥ 20%
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is True


@pytest.mark.asyncio
async def test_compact_blocked_when_running(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000002a"
    _write_task(tmp_path, tid, content_chars=500)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=True)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False


@pytest.mark.asyncio
async def test_compact_blocked_low_content(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000003a"
    _write_task(tmp_path, tid, content_chars=50)    # 5% порога < 20%
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config=_chat_cfg())
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False


@pytest.mark.asyncio
async def test_compact_blocked_in_responses(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    tid = "cmp00000000000000000000000000004a"
    _write_task(tmp_path, tid, content_chars=500)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "responses"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        runner = _Runner(running=False)
        pilot.app.active_runner = runner
        scr._cmd_compact()
        assert runner.compacted is False
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_cmd_compact.py -q`
Expected: FAIL — `AttributeError: ... '_cmd_compact'`.

- [ ] **Step 3: Реализовать `_cmd_compact`**

В [src/tui/screens/main.py](../../../src/tui/screens/main.py) добавить метод (рядом с `_cmd_init`):

```python
    def _cmd_compact(self) -> None:
        """/compact — принудительно сжать сессию (гарды: режим/простой/контент)."""
        cfg = getattr(self.app, "_config", None) or {}
        if str(cfg.get("backend_api_mode")) != "chat_completions":
            self._mount_trace_row(prefix="[stopped] ",
                                  content="/compact недоступен в режиме responses",
                                  kind_class="line-meta")
            return
        tid = self._active_task_id
        runner = getattr(self.app, "active_runner", None)
        if tid is None or self._store is None:
            self._mount_trace_row(prefix="[stopped] ", content="/compact: нет активной сессии",
                                  kind_class="line-meta")
            return
        if (runner is not None and runner.is_running()) \
                or self._task_status_badge() in ("[running]", "[asking]"):
            self._mount_trace_row(prefix="[stopped] ",
                                  content="/compact: дождитесь паузы или /stop",
                                  kind_class="line-meta")
            return
        from summarize.config import parse_summary_trigger_chars
        threshold = parse_summary_trigger_chars(cfg)
        chars = calc_history_chars_for_trigger(self._store.read_live_messages(tid))
        if threshold > 0 and chars < 0.20 * threshold:
            pct = round(100 * chars / threshold)
            self._mount_trace_row(
                prefix="[stopped] ",
                content=f"/compact: контекст ещё небольшой ({pct}% порога) — сжимать нечего",
                kind_class="line-meta")
            return
        if runner is None:
            runner = self.app.get_runner()
        runner.compact_now(tid, self.app._system)
```

(`self.app._system` — система (как в `runner.start(..., system=self.app._system)`); `self.app.get_runner()` — существующая фабрика раннера.)

- [ ] **Step 4: Подключить диспетч команды**

В `main.py` найти обработчик команд (где `if name == "init": self._cmd_init()` и т.п.) и добавить рядом:

```python
        if name == "compact":
            self._cmd_compact()
            return
```

- [ ] **Step 5: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_cmd_compact.py -q`
Expected: PASS (4).

- [ ] **Step 6: Commit**

```bash
git add src/tui/screens/main.py tests/tui/test_cmd_compact.py
git commit -m "tui: /compact — гарды (режим/простой/контент) + запуск форс-сжатия"
```

---

## Task 5: Регистрация команды `/compact`

**Files:**
- Modify: `src/tui/commands.py`
- Test: `tests/tui/test_commands.py`

**Interfaces:**
- Consumes: диспетч `_cmd_compact` (T4).

- [ ] **Step 1: Написать падающий тест**

В [tests/tui/test_commands.py](../../../tests/tui/test_commands.py) добавить:

```python
def test_compact_command_registered_with_alias():
    from tui.commands import build_default_registry
    reg = build_default_registry()
    cmd = reg.lookup("compact")
    assert cmd is not None
    assert cmd.description == "сжать контекст сессии сейчас"
    # алиас /сжать ведёт на ту же команду
    assert reg.lookup("сжать") is cmd
```

- [ ] **Step 2: Запустить — убедиться, что падает**

Run: `uv run pytest tests/tui/test_commands.py::test_compact_command_registered_with_alias -q`
Expected: FAIL — `lookup("compact")` is None.

- [ ] **Step 3: Зарегистрировать команду**

В [src/tui/commands.py](../../../src/tui/commands.py) в `build_default_registry`, в список `cmds`, добавить (рядом с `init`):

```python
        Command("compact", ("сжать",), "", "сжать контекст сессии сейчас"),
```

- [ ] **Step 4: Запустить — убедиться, что проходит**

Run: `uv run pytest tests/tui/test_commands.py -q`
Expected: PASS (новый тест + существующие; keep-set `test_default_registry_is_exactly_keep_set` — обнови ожидаемый набор, добавив `compact`, если этот тест перечисляет имена).

- [ ] **Step 5: Commit**

```bash
git add src/tui/commands.py tests/tui/test_commands.py
git commit -m "tui: команда /compact (+алиас /сжать) в реестре"
```

---

## Финальная проверка

- [ ] `pytest tests/test_force_compact_once.py tests/test_compact_thread.py tests/test_summary_pipeline.py -q` — суммаризация цела.
- [ ] `uv run pytest tests/tui -q` — без новых падений.
- [ ] (Ручное) В chat-сессии после работы: `/compact` → плашка «сжимаю контекст…» → маркер «контекст сжат» в трейсе, индикатор падает. `/compact` сразу повторно → «контекст ещё небольшой (NN%)». `/compact` во время прогона → «дождитесь паузы». В responses → «недоступен».

---

## Self-Review

**Spec coverage (Component 2):** имя+алиас → T5; off-thread механика → T3; force-сжатие (та же `run_summary_compaction`) → T1+T2; гарды режим/простой/контент(20%) → T4; плашка «сжимаю…» — авто через `_summarizing_flag` (T1). ✓

**Placeholder scan:** плейсхолдеров нет. Имена сверены: `AgentRunner(executor=…/executor_factory=…)`; система — `self.app._system`; State в тесте T2 мокается (избегаем WMI). Известный риск: keep-set тест реестра (`test_default_registry_is_exactly_keep_set`) перечисляет имена команд — T5 Step 4 обновляет его (+`compact`).

**Type consistency:** `force_compact_once(*, trigger_reason="manual")` (T1) ← зовётся в `compact_thread` (T2). `compact_thread(thread_id, system=None)->bool` (T2) ← зовётся в `compact_now` (T3). `compact_now(thread_id, system)->bool` (T3) ← зовётся в `_cmd_compact` (T4). Команда `compact`+алиас `сжать` (T5) ← диспетч `_cmd_compact` (T4). Согласовано.
