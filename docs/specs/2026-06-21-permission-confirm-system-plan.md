# Permission / Confirm System — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать confirm-подтверждениям память решений (run + session) и 3-значный выбор (once|session|deny), чтобы «Нет» не приводил к повторным плашкам, а «в этой сессии» переживало прогоны.

**Architecture:** Новый изолированный `PermissionStore` (run-кэш + session-правила, by-reference на `state.shared.confirm_rules`). `confirm_port.request_confirm` — единственная точка политики: получает `decision_key`, резолвит из store, на промахе спрашивает 3-значный выбор, записывает решение. Seed store на `ctx` в `AgentExecutor.run`. Гарды передают `decision_key` + инструктивный deny. TUI confirm-канал расширяется из bool в строковый `ConfirmChoice`, баннер — вертикальный 3-кнопочный с ↑/↓.

**Tech Stack:** Python ≥3.10, pytest, Textual (TUI). Абсолютные импорты от `src/`.

**Спека:** `docs/specs/2026-06-21-permission-confirm-system-design.md` (§1–§8).

## Global Constraints

- Абсолютные импорты от `src/` (`from security.permission_store import ...`), без относительных и без `src.`-префикса.
- TDD: тест → провал → минимальная реализация → проход → коммит.
- TUI-тесты гонять **только** `uv run pytest ...` (extra `[dev-tui]` в `.venv/`). Прочие тесты — `pytest`/`uv run pytest`.
- Коммиты лаконичные, рус/смешанный язык, **без** упоминаний AI/Claude/Anthropic и без `Co-Authored-By`.
- `ConfirmChoice` — строковые литералы `"once" | "session" | "deny"`; константы `CONFIRM_ONCE/CONFIRM_SESSION/CONFIRM_DENY` в `security/confirm_port.py`.
- `decision_key` формат: `"<action>:<value>"`, action ∈ `{write, read, url, command}`.
- 5 пред-существующих Windows-падений (`test_path_policy.py::test_check_write_path_blocks_symlink_escape`, `::test_is_within_root_supports_case_insensitive_comparison`, `test_read_guard.py::test_relative_path_resolves_against_launch_root`, `::test_gc_skills_exception_allowed`, `::test_wildcard_root_allow_by_default`) — известны, игнорировать; новых не добавлять.
- CLI/headless поведение не делать более пермиссивным: без tty `request_confirm` всегда даёт `deny`.

---

### Task 1: PermissionStore (память решений)

**Files:**
- Create: `src/security/permission_store.py`
- Test: `tests/security/test_permission_store.py`

**Interfaces:**
- Consumes: ничего.
- Produces:
  - `PermissionStore(session_rules: dict[str, bool] | None = None)` — `session_rules` берётся **по ссылке** (мутации видны снаружи; для изоляции передавать копию).
  - `.decision_for(key: str) -> bool | None` — True=allow, False=deny, None=спросить; session приоритетнее run.
  - `.record(key: str, allow: bool, *, session: bool) -> None` — всегда пишет в run; при `session=True` — ещё и в session.
  - `.session_rules() -> dict[str, bool]` — текущий session-словарь (тот же объект).

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_permission_store.py
from security.permission_store import PermissionStore


def test_unknown_key_returns_none():
    s = PermissionStore()
    assert s.decision_for("write:/x") is None


def test_record_run_then_decision_for():
    s = PermissionStore()
    s.record("write:/x", True, session=False)
    assert s.decision_for("write:/x") is True
    s.record("write:/y", False, session=False)
    assert s.decision_for("write:/y") is False


def test_session_takes_priority_over_run():
    s = PermissionStore()
    s.record("write:/x", False, session=False)  # run says deny
    s.record("write:/x", True, session=True)    # session says allow
    assert s.decision_for("write:/x") is True


def test_session_record_writes_through_to_passed_dict():
    shared: dict[str, bool] = {}
    s = PermissionStore(session_rules=shared)
    s.record("url:http://a", True, session=True)
    assert shared == {"url:http://a": True}        # by-reference write-through
    s.record("read:/z", True, session=False)
    assert "read:/z" not in shared                 # run-scope не уходит в session


def test_namespace_isolation_is_just_distinct_keys():
    s = PermissionStore()
    s.record("write:/p", True, session=False)
    assert s.decision_for("read:/p") is None       # другой action — другой ключ
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/security/test_permission_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'security.permission_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/security/permission_store.py
from __future__ import annotations

from typing import Optional


class PermissionStore:
    """Память решений confirm за прогон (run) + за сессию/тред (session).

    Ключ — "<action>:<value>" (write:<path>, read:<path>, url:<url>, command:<cmd>).
    Матч — точное равенство ключа (Mid; глобы — Full). session приоритетнее run.

    session_rules берётся ПО ССЫЛКЕ: в AgentExecutor сюда передаётся
    state.shared.confirm_rules, поэтому record(session=True) мутирует его —
    и существующая персистентность shared сохранит правило в meta.
    """

    def __init__(self, session_rules: Optional[dict] = None) -> None:
        self._session: dict[str, bool] = session_rules if session_rules is not None else {}
        self._run: dict[str, bool] = {}

    def decision_for(self, key: str) -> Optional[bool]:
        if key in self._session:
            return bool(self._session[key])
        if key in self._run:
            return self._run[key]
        return None

    def record(self, key: str, allow: bool, *, session: bool) -> None:
        self._run[key] = allow
        if session:
            self._session[key] = allow

    def session_rules(self) -> dict:
        return self._session
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/security/test_permission_store.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add src/security/permission_store.py tests/security/test_permission_store.py
git commit -m "feat(security): PermissionStore — память решений confirm (run + session)"
```

---

### Task 2: confirm_port — decision_key, store-резолв, 3-значный выбор

**Files:**
- Modify: `src/security/confirm_port.py`
- Modify: `src/security/path_policy.py` (добавить `prompt_user_confirmation_choice`)
- Modify: `tests/security/test_confirm_port.py` (под новый контракт)
- Modify: `tests/tools/exec/test_command_guard_wire.py` (перепатчить confirm-мок)
- Test: добавить кейсы в `tests/security/test_confirm_port.py`

**Interfaces:**
- Consumes: `PermissionStore.decision_for/record` (Task 1); `ctx.permission_store` (Task 3, опционально — может отсутствовать).
- Produces:
  - `CONFIRM_ONCE="once"`, `CONFIRM_SESSION="session"`, `CONFIRM_DENY="deny"` в `security/confirm_port.py`.
  - `request_confirm(*, title, question, fields, ctx, decision_key: str | None = None) -> bool` — возвращает bool гарду (как раньше), но внутри резолвит store + 3-значный выбор.
  - `prompt_user_confirmation_choice(*, title, fields, question) -> str` в `security/path_policy.py` — CLI-вариант, возвращает `once|session|deny`.
  - Контракт `ctx.tui_confirm(title=, question=, fields=) -> str` (одно из `once|session|deny`) — реализуется в Task 5.

- [ ] **Step 1: Write the failing tests**

```python
# tests/security/test_confirm_port.py — ДОБАВИТЬ (не удаляя осмысленных старых):
import types
from security import confirm_port as cp
from security.permission_store import PermissionStore


def _tui_ctx(choice, store=None):
    # is_tui ctx с tui_confirm, возвращающим заданный choice; счётчик вызовов.
    calls = {"n": 0}
    def tui_confirm(*, title, question, fields):
        calls["n"] += 1
        return choice
    ctx = types.SimpleNamespace(
        is_tui=True, tui_confirm=tui_confirm, config_dict={}, permission_store=store,
    )
    return ctx, calls


def test_choice_once_allows_and_records_run_only():
    store = PermissionStore()
    ctx, calls = _tui_ctx("once", store)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is True
    assert calls["n"] == 1
    assert store.session_rules() == {}                 # once не в session
    # повтор того же ключа — без нового вопроса (run-кэш)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is True
    assert calls["n"] == 1


def test_choice_session_allows_and_records_session():
    store = PermissionStore()
    ctx, calls = _tui_ctx("session", store)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is True
    assert store.session_rules() == {"write:/x": True}


def test_choice_deny_blocks_and_caches_run():
    store = PermissionStore()
    ctx, calls = _tui_ctx("deny", store)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is False
    # повтор — без нового вопроса, всё ещё deny (anti-loop)
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is False
    assert calls["n"] == 1


def test_no_decision_key_always_asks():
    store = PermissionStore()
    ctx, calls = _tui_ctx("once", store)
    cp.request_confirm(title="t", question="q", fields=[], ctx=ctx)
    cp.request_confirm(title="t", question="q", fields=[], ctx=ctx)
    assert calls["n"] == 2                              # без ключа — кэш не работает


def test_headless_fail_closed_without_tty(monkeypatch):
    monkeypatch.setattr(cp.sys, "stdin", types.SimpleNamespace(isatty=lambda: False))
    ctx = types.SimpleNamespace(config_dict={}, permission_store=PermissionStore())
    assert cp.request_confirm(title="t", question="q", fields=[], ctx=ctx, decision_key="write:/x") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/security/test_confirm_port.py -v`
Expected: FAIL (`request_confirm() got an unexpected keyword argument 'decision_key'` и/или отсутствие `once` ветки)

- [ ] **Step 3: Implement — `prompt_user_confirmation_choice` в path_policy.py**

Добавить рядом с `prompt_user_confirmation` (НЕ удаляя её):

```python
# src/security/path_policy.py
def prompt_user_confirmation_choice(
    *,
    title: str,
    fields: list[tuple[str, Any]],
    question: str,
) -> str:
    """CLI-подтверждение с 3 вариантами: y=once, s=session, n/прочее=deny.
    Auto-deny если stdin не tty (фоновый/CI запуск)."""
    if not sys.stdin or not sys.stdin.isatty():
        return "deny"
    lines = [f"\n{title}: confirmation required."]
    for label, value in fields:
        lines.append(f"{label}: {value}")
    lines.append(f"{question} [y=раз / s=сессия / N=нет]: ")
    try:
        print("\n".join(lines), file=sys.stderr, end="")
        sys.stderr.flush()
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "deny"
    if answer in {"y", "yes"}:
        return "once"
    if answer in {"s", "session"}:
        return "session"
    return "deny"
```

- [ ] **Step 4: Implement — confirm_port.py**

```python
# src/security/confirm_port.py
from __future__ import annotations

import sys
from typing import Any

from security.path_policy import prompt_user_confirmation_choice

CONFIRM_ONCE = "once"
CONFIRM_SESSION = "session"
CONFIRM_DENY = "deny"
_VALID_CHOICES = {CONFIRM_ONCE, CONFIRM_SESSION, CONFIRM_DENY}


def _ask_human_enabled(ctx: Any) -> bool:
    cfg = getattr(ctx, "config_dict", None) or {}
    stop = cfg.get("stop_tools") or []
    return "ask_human" not in stop


def confirm_available(ctx: Any) -> bool:
    if getattr(ctx, "is_tui", False):
        return True
    if not _ask_human_enabled(ctx):
        return False
    stdin = getattr(sys, "stdin", None)
    return bool(stdin and stdin.isatty())


def _ask_choice(ctx: Any, *, title: str, question: str, fields: list) -> str:
    if not confirm_available(ctx):
        return CONFIRM_DENY
    if getattr(ctx, "is_tui", False) and getattr(ctx, "tui_confirm", None):
        choice = ctx.tui_confirm(title=title, question=question, fields=fields)
        return choice if choice in _VALID_CHOICES else CONFIRM_DENY
    return prompt_user_confirmation_choice(title=title, fields=fields, question=question)


def request_confirm(
    *, title: str, question: str, fields: list[tuple[str, object]], ctx: Any,
    decision_key: str | None = None,
) -> bool:
    store = getattr(ctx, "permission_store", None)
    if store is not None and decision_key is not None:
        cached = store.decision_for(decision_key)
        if cached is not None:
            return cached
    choice = _ask_choice(ctx, title=title, question=question, fields=fields)
    allow = choice in (CONFIRM_ONCE, CONFIRM_SESSION)
    if store is not None and decision_key is not None:
        store.record(decision_key, allow, session=(choice == CONFIRM_SESSION))
    return allow
```

- [ ] **Step 5: Re-point `test_command_guard_wire.py`**

Эти тесты патчат `security.confirm_port.prompt_user_confirmation` (теперь не используется в confirm_port). Перепатчить на `request_confirm` напрямую:

```python
# tests/tools/exec/test_command_guard_wire.py — заменить обе строки monkeypatch:
# было: monkeypatch.setattr("security.confirm_port.prompt_user_confirmation", lambda **k: True)
monkeypatch.setattr("security.confirm_port.request_confirm", lambda **k: True)
# было: ... lambda **k: False
monkeypatch.setattr("security.confirm_port.request_confirm", lambda **k: False)
```

(Если в этих тестах есть прямой `import` модерации/гарда, мок `request_confirm` ставить на тот модуль, откуда он реально вызывается — проверить грепом `request_confirm` в задействованном гарде; для command_guard это `security.command_guard.request_confirm` при `from security.confirm_port import request_confirm`. Использовать `monkeypatch.setattr("security.command_guard.request_confirm", ...)`.)

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/security/test_confirm_port.py tests/tools/exec/test_command_guard_wire.py -v`
Expected: PASS (новые confirm_port-кейсы + command_guard_wire зелёные)

- [ ] **Step 7: Commit**

```bash
git add src/security/confirm_port.py src/security/path_policy.py tests/security/test_confirm_port.py tests/tools/exec/test_command_guard_wire.py
git commit -m "feat(security): confirm_port — decision_key + 3-значный выбор (once|session|deny) + store-резолв"
```

---

### Task 3: Персистентность session-правил + seed store на ctx

**Files:**
- Modify: `src/states.py` (поле `confirm_rules` в `SharedStateData`)
- Modify: `src/persistence/schema.py` (`serialize_shared_block`)
- Modify: `src/persistence/thread_meta_mapper.py` (`shared_from_meta`)
- Modify: `src/agent_executor.py` (seed `ctx.permission_store` после создания state, ~строка 224)
- Test: `tests/security/test_permission_persistence.py`

**Interfaces:**
- Consumes: `PermissionStore` (Task 1).
- Produces: `state.shared.confirm_rules: dict[str, bool]`; `ctx.permission_store` живёт весь прогон, его session-правила = `state.shared.confirm_rules` (by-reference).

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_permission_persistence.py
from states import SharedStateData
from persistence.schema import serialize_shared_block
from persistence.thread_meta_mapper import ThreadMetaMapper


def test_confirm_rules_round_trips_through_meta():
    shared = SharedStateData()
    shared.confirm_rules["write:/x"] = True
    block = serialize_shared_block(shared)
    assert block["confirm_rules"] == {"write:/x": True}
    restored = ThreadMetaMapper.shared_from_meta({"shared": block})
    assert restored.confirm_rules == {"write:/x": True}


def test_shared_defaults_confirm_rules_empty():
    assert SharedStateData().confirm_rules == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/security/test_permission_persistence.py -v`
Expected: FAIL (`AttributeError: 'SharedStateData' object has no attribute 'confirm_rules'`)

- [ ] **Step 3: Implement — `SharedStateData` (states.py)**

Добавить поле в dataclass `SharedStateData` (рядом с `fetch_url_cache`):

```python
    confirm_rules: Dict[str, bool] = field(default_factory=dict)
```

- [ ] **Step 4: Implement — `serialize_shared_block` (schema.py)**

В возвращаемый dict добавить ключ `confirm_rules`:

```python
    confirm_rules = getattr(shared, "confirm_rules", None)
    rules: dict[str, Any] = dict(confirm_rules) if isinstance(confirm_rules, Mapping) else {}
    # ... в return-словарь добавить:
    #   "confirm_rules": rules,
```

(Вставить `rules` в тело и добавить `"confirm_rules": rules` в возвращаемый словарь рядом с `fetch_url_cache`.)

- [ ] **Step 5: Implement — `shared_from_meta` (thread_meta_mapper.py)**

В конструктор `SharedStateData(...)` (строка ~28) добавить аргумент:

```python
                confirm_rules=dict(shared_raw.get("confirm_rules") or {}),
```

- [ ] **Step 6: Implement — seed на ctx (agent_executor.py)**

После блока `with self._lock: self._state = state` (≈ строка 224, ПОСЛЕ присвоения `self._state = state`) добавить:

```python
        # Permission memory: store оборачивает state.shared.confirm_rules ПО ССЫЛКЕ —
        # session-правила пишутся прямо в shared и сохраняются обычной персистентностью.
        from security.permission_store import PermissionStore
        setattr(
            self._ctx, "permission_store",
            PermissionStore(session_rules=state.shared.confirm_rules),
        )
```

- [ ] **Step 7: Run tests**

Run: `uv run pytest tests/security/test_permission_persistence.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add src/states.py src/persistence/schema.py src/persistence/thread_meta_mapper.py src/agent_executor.py tests/security/test_permission_persistence.py
git commit -m "feat(security): персистентность session-правил confirm (shared.confirm_rules) + seed store на ctx"
```

---

### Task 4: Гарды — decision_key + инструктивный deny (6 сайтов)

**Files:**
- Modify: `src/security/path_policy.py` (write_guard ask + `_ask_user_confirmation_for_write`)
- Modify: `src/tools/exec/download_file.py` (write_guard ask загрузок)
- Modify: `src/security/url_policy.py` (ask)
- Modify: `src/security/read_guard.py` (ask)
- Modify: `src/security/command_moderation.py` (`_ask_user_confirmation` + decline reason)
- Modify: `src/security/command_guard.py` (tripwire ask)
- Test: `tests/security/test_confirm_memory_guards.py`

**Interfaces:**
- Consumes: `request_confirm(..., decision_key=...)` (Task 2); `ctx.permission_store` (Task 3).
- Produces: каждый гард передаёт `decision_key`; отклонение возвращает агенту инструктивный текст (§5 спеки).

- [ ] **Step 1: Write the failing test (write_guard как репрезентативный)**

```python
# tests/security/test_confirm_memory_guards.py
import types
from security import confirm_port as cp
from security.permission_store import PermissionStore


def _tui_ctx(choice):
    calls = {"n": 0}
    def tui_confirm(*, title, question, fields):
        calls["n"] += 1
        return choice
    ctx = types.SimpleNamespace(
        is_tui=True, tui_confirm=tui_confirm, config_dict={},
        permission_store=PermissionStore(),
    )
    return ctx, calls


def test_write_guard_decline_caches_run_and_instructs(monkeypatch, tmp_path):
    """[N] на запись вне периметра: повтор не переспрашивает; сообщение инструктивно."""
    from security import path_policy as pp

    ctx, calls = _tui_ctx("deny")
    # минимальный «тул», который пишет вне периметра
    import os
    outside = os.path.join(str(tmp_path.parent), "outside.txt")
    captured = {}

    class _Tool:
        _ctx = ctx
        @pp.enforce_write_guard(path_arg="path")
        def write(self, path):  # noqa
            return "WROTE"
        def _write_guard_blocked_result(self, err, *a, **k):
            captured["err"] = err
            return "BLOCKED"

    # NB: точная сигнатура декоратора/контекста — взять из path_policy; тест адаптировать
    # к фактическому API enforce_write_guard (см. существующий tests/security/test_write_guard_protected.py).
    # Ключевые проверки:
    #   1) первый вызов → tui_confirm вызван (calls["n"] == 1), результат BLOCKED;
    #   2) повторный вызов того же пути → calls["n"] всё ещё 1 (run-кэш), снова BLOCKED;
    #   3) captured["err"]["message"] содержит "Do NOT retry" и "ask the user".
```

> Примечание исполнителю: точную конструкцию `enforce_write_guard`/контекста копировать из
> `tests/security/test_write_guard_protected.py` (там уже строится guard-ctx и TUI-namespace).
> Тест должен проверить три пункта выше.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/security/test_confirm_memory_guards.py -v`
Expected: FAIL (decision_key не передаётся → кэш не работает; сообщение старое «User declined out-of-perimeter write.»)

- [ ] **Step 3: Implement — path_policy.py write_guard**

В `_ask_user_confirmation_for_write` добавить параметр `decision_key`, пробросить в `request_confirm`; на call-site (ветка `if wg_mode == "ask":`) передать `decision_key=f"write:{candidate_path}"`. Сообщение отказа (ветка `if not confirmed:` / `exc.error[...]`) заменить на:

```python
                exc.error["type"] = "write_guard_cancelled_by_user"
                exc.error["message"] = (
                    f"User declined the write to '{candidate_path}'. Do NOT retry the same "
                    "write, and do NOT silently write somewhere else — ask the user how to "
                    "proceed, or stop and report that it was declined."
                )
```

- [ ] **Step 4: Implement — остальные 5 сайтов (тот же паттерн)**

Передать `decision_key` и заменить decline-сообщение (меняется только глагол):

- `download_file.py` (write_guard загрузок): `decision_key=f"write:{dest}"`; сообщение как write_guard выше с `dest`.
- `url_policy.py` (ask): `decision_key=f"url:{decision.url}"`; decline-сообщение:
  `f"User declined the request to '{decision.url}'. Do NOT retry the same URL, and do NOT silently fetch a different URL — ask the user or stop."`
- `read_guard.py` (ask): `decision_key=f"read:{<resolved path>}"`; decline:
  `"...Do NOT retry the same read, and do NOT silently read a different file — ask the user or stop."`
- `command_moderation.py` (`_ask_user_confirmation` → `request_confirm`, и call-site передаёт `decision_key=f"command:{payload}"`): decline reason:
  `"User declined the command. Do NOT retry the same command, and do NOT silently run a modified command — ask the user or stop."`
- `command_guard.py` (tripwire ask): `decision_key=f"command:{payload}"`.

> Для каждого: `request_confirm(..., ctx=<wired ctx>, decision_key=<key>)`. ctx — тот же, что уже передаётся (`context.ctx` / `tool_ctx`).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/security/test_confirm_memory_guards.py tests/security -k "write_guard or url_policy or read_guard or moderation" -v`
Expected: PASS (новый тест + не сломаны существующие guard-тесты; 5 известных Windows-падений игнор)

- [ ] **Step 6: Commit**

```bash
git add src/security/path_policy.py src/tools/exec/download_file.py src/security/url_policy.py src/security/read_guard.py src/security/command_moderation.py src/security/command_guard.py tests/security/test_confirm_memory_guards.py
git commit -m "feat(security): гарды передают decision_key + инструктивный deny (не повторяй/не подменяй молча)"
```

---

### Task 5: TUI confirm-канал → 3-значный ConfirmChoice

**Files:**
- Modify: `src/tui/services/agent_runner.py` (`make_tui_confirm`/`submit_confirm_reply`/`cancel_confirm`)
- Modify: `tests/tui/test_agent_runner.py` (под choice)
- Modify: `tests/tui/test_confirm_wiring.py` (интеграция возвращает choice)

**Interfaces:**
- Consumes: контракт `tui_confirm -> str` из Task 2.
- Produces:
  - `AgentRunner.make_tui_confirm(present) -> Callable[..., str]` — замыкание возвращает `"once"|"session"|"deny"`.
  - `AgentRunner.submit_confirm_reply(choice: str) -> None` — UI кладёт выбор.
  - `cancel_confirm()` → выбор `"deny"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui/test_agent_runner.py — ЗАМЕНИТЬ confirm-тесты на choice-версии:
def test_confirm_submit_session_returns_session():
    from tui.services.agent_runner import AgentRunner
    r = AgentRunner(executor=type("E", (), {"stop_run": lambda s: None})())
    import threading
    def present(t, q, f):
        threading.Thread(target=lambda: r.submit_confirm_reply("session"), daemon=True).start()
    confirm = r.make_tui_confirm(present)
    assert confirm(title="t", question="q", fields=[]) == "session"


def test_confirm_cancel_returns_deny():
    from tui.services.agent_runner import AgentRunner
    r = AgentRunner(executor=type("E", (), {"stop_run": lambda s: None})())
    import threading
    def present(t, q, f):
        threading.Thread(target=r.cancel_confirm, daemon=True).start()
    confirm = r.make_tui_confirm(present)
    assert confirm(title="t", question="q", fields=[]) == "deny"
```

(Старые bool-тесты confirm-канала в этом файле — обновить: `submit_confirm_reply(True)` → `submit_confirm_reply("once")`, ожидание `is True` → `== "once"`; `submit_confirm_reply(False)` → `"deny"`; `stop()`-unblock ожидает `"deny"`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/tui/test_agent_runner.py -k confirm -v`
Expected: FAIL (возвращается bool, не строка)

- [ ] **Step 3: Implement — agent_runner.py**

```python
# __init__: заменить
self._confirm_result: bool = False
# на
self._confirm_choice: str = "deny"

# make_tui_confirm: закрытие возвращает str
    def _tui_confirm(*, title: str, question: str, fields: list) -> str:
        self._confirm_event.clear()
        self._confirm_choice = "deny"
        present(title, question, fields)
        self._confirm_event.wait()
        return self._confirm_choice

# submit_confirm_reply
    def submit_confirm_reply(self, choice: str) -> None:
        """UI кладёт выбор: once|session|deny. Разблокирует агент-поток."""
        self._confirm_choice = choice
        self._confirm_event.set()

# cancel_confirm
    def cancel_confirm(self) -> None:
        self._confirm_choice = "deny"
        self._confirm_event.set()
```

Сигнатуру `make_tui_confirm(...) -> Callable[..., str]` обновить в аннотации.

- [ ] **Step 4: Update `test_confirm_wiring.py`**

Интеграционный тест `test_confirm_full_chain_via_run_test`: `pilot.press("y")` теперь даёт `"once"` — поменять ассерт `val is True` на `val == "once"`. Re-entrancy тест: decline → `"deny"`, approve → `"once"`. Unit-тест `_present_confirm` остаётся (он про передачу title/question/fields).

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/tui/test_agent_runner.py tests/tui/test_confirm_wiring.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tui/services/agent_runner.py tests/tui/test_agent_runner.py tests/tui/test_confirm_wiring.py
git commit -m "feat(tui): confirm-канал 3-значный (ConfirmChoice once|session|deny)"
```

---

### Task 6: TUI баннер — вертикаль, 3 варианта, ↑/↓, обработка [S]

**Files:**
- Modify: `src/tui/widgets/confirm_banner.py` (3 варианта, вертикаль, ↑/↓ через `selected`)
- Modify: `src/tui/screens/main.py` (`_answer_confirm(choice)` + перехват клавиш `Y/S/N/↑/↓/Enter/Esc`)
- Modify: `src/tui/styles.tcss` (если нужен отступ строк вариантов)
- Modify: `tests/tui/test_confirm_banner.py`, `tests/tui/test_confirm_banner_flow.py`, `tests/tui/test_snapshot_confirm_banner.py`

**Interfaces:**
- Consumes: `AgentRunner.submit_confirm_reply(choice: str)` (Task 5).
- Produces: баннер с `selected ∈ {0,1,2}` → `once|session|deny`; экран зовёт `submit_confirm_reply(choice)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/tui/test_confirm_banner.py — заменить _OPTIONS-зависимые места; добавить:
def test_banner_has_three_options():
    from tui.widgets.confirm_banner import _OPTIONS
    assert [k for k, _ in _OPTIONS] == ["[Y]", "[S]", "[N]"]


# tests/tui/test_confirm_banner_flow.py — interaction (mirror существующих):
#  - default Enter → submit_confirm_reply("once")
#  - press "s" → "session";  press "n"/"escape" → "deny"
#  - press "down" затем Enter → "session" (selected == 1)
```

(В flow-тестах stub-runner записывает `submit_confirm_reply`-аргумент; ассертить строку.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/tui/test_confirm_banner.py tests/tui/test_confirm_banner_flow.py -v`
Expected: FAIL (2 варианта вместо 3; `_answer_confirm` принимает bool)

- [ ] **Step 3: Implement — confirm_banner.py**

```python
# _OPTIONS: три варианта (keycap, label) — вертикально
_OPTIONS = [
    ("[Y]", "Разрешить"),
    ("[S]", "Разрешить в этой сессии — не спрашивать снова"),
    ("[N]", "Нет — отклонить"),
]

# _render_options: вертикальный рендер (каждая строка отдельно), подсвеченная — teal.
def _render_options(selected: int) -> "Text":
    from rich.text import Text
    t = Text()
    for idx, (key, label) in enumerate(_OPTIONS):
        if idx > 0:
            t.append("\n")
        prefix = "▸ " if idx == selected else "  "
        if idx == selected:
            t.append(f"{prefix}{key} {label} ", style=f"bold {BG} on {ACCENT}")
        else:
            t.append(prefix)
            t.append(f"{key}", style=ACCENT)
            t.append(f" {label}", style=DIM)
    return t

# move_selection: clamp по len(_OPTIONS) (уже так), обновляет _options_static.

# Новый helper: маппинг selected → choice
def choice_for(selected: int) -> str:
    return ("once", "session", "deny")[selected] if 0 <= selected < 3 else "deny"
```

(`choice_for` положить функцией модуля рядом с `_render_options`; экран использует `banner.selected` + `choice_for`.)

- [ ] **Step 4: Implement — main.py перехват клавиш + `_answer_confirm`**

Блок `if self._confirm_active:` в `on_key` (заменить тело под 3 варианта и ↑/↓):

```python
        if self._confirm_active:
            key = event.key
            banner = self._confirm_banner
            if key == "up" and banner is not None:
                banner.move_selection(-1)
            elif key == "down" and banner is not None:
                banner.move_selection(1)
            elif key == "enter":
                from tui.widgets.confirm_banner import choice_for
                self._answer_confirm(choice_for(banner.selected) if banner is not None else "deny")
            elif key in ("y", "Y"):
                self._answer_confirm("once")
            elif key in ("s", "S"):
                self._answer_confirm("session")
            elif key in ("n", "N", "escape"):
                self._answer_confirm("deny")
            event.stop(); event.prevent_default(); return
```

`_answer_confirm` — принять choice:

```python
    def _answer_confirm(self, choice: str) -> None:
        runner = getattr(self.app, "active_runner", None)
        if runner is not None and hasattr(runner, "submit_confirm_reply"):
            runner.submit_confirm_reply(choice)
        self._hide_confirm_banner()
```

(Стоп-путь `_cmd_stop`/`_reset_render_state` уже зовут `cancel_confirm()` — менять не нужно.)

- [ ] **Step 5: Регенерировать снапшот + прогнать**

```bash
uv run pytest tests/tui/test_snapshot_confirm_banner.py --snapshot-update -q
```
Глазами проверить `.raw`-бейслайн: три строки вариантов, `[Y]` подсвечен teal. Затем:

Run: `uv run pytest tests/tui -k "confirm or banner" -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/tui/widgets/confirm_banner.py src/tui/screens/main.py src/tui/styles.tcss tests/tui/test_confirm_banner.py tests/tui/test_confirm_banner_flow.py tests/tui/test_snapshot_confirm_banner.py "tests/tui/__snapshots__/test_snapshot_confirm_banner/test_confirm_banner_snapshot.raw"
git commit -m "feat(tui): confirm-баннер вертикальный 3-кнопочный (Разрешить / в сессии / Нет) + ↑/↓"
```

---

## Финальная проверка (после всех задач)

- `uv run pytest tests/security tests/tui -k "confirm or guard or url_policy or read_guard or moderation or banner or permission or wiring" -q` — зелено (кроме 5 известных Windows-падений).
- Живой TUI-прогон (ручной): запись вне периметра → вертикальный баннер 3-кнопочный; `[N]` → агент не долбит (повтор без плашки) + инструктивный текст; `[S]` → в этом треде больше не спрашивает; новый запрос после `[N]` → спрашивает снова.
- CLI-tty: stdin-промпт `[y/s/N]`. Headless: fail-closed (без регресса).

**Известный лимит Mid (намеренно):** seed `ctx.permission_store` делается только для главного
executor (Task 3). Субагентский `child_ctx` (`tools/subagents/runner.py`) store не получает →
`request_confirm` видит `store=None` → кэша нет, а субагент-прогон обычно headless → fail-closed
deny. Проводка субагентского store — рефайнмент вне scope этого плана.
