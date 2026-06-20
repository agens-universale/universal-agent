# `.gc` Protection + Command-Guard + Memory Backup — Implementation Plan (Спека B)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Закрыть `.gc` и файлы инструкций от агентских egress на трёх дверях (read/write/execute), с floor-защитой в коде + opt-out, и пережить удаление через бэкап памяти. Фиксит баг TSKBCKGRNDGNT-2.

**Architecture:** Потребляет фундамент Плана A (`protected_paths()`, confirm-порт). Добавляет: классификацию пути → решение (deny/ask/allow), карв-ауты в `write_guard`/`read_guard`, новый детерминированный command-guard (tripwire) + инжект protected-путей в промпт судьи, floor-конфиг `security.protected_paths`, бэкап long memory в `gc_state`. C1 Часть 2 — `command_moderation` при поломке судьи зовёт confirm-порт.

**Tech Stack:** Python ≥3.10, pytest, абсолютные импорты от `src/`.

**Предусловие:** План A внедрён (есть `security/protected_paths.py::protected_paths/ProtectedSet`, `security/confirm_port.py::request_confirm/confirm_available`).

## Global Constraints

- Импорты — абсолютные от `src/`. Тесты — pytest, прямые `assert`, реальные пути (`tmp_path`).
- `protected_paths()` — единый source-of-truth; конфиг путей **не дублирует** (только тумблер + `extra`).
- Floor применяется **всегда** (пока не выключен явно), **независимо** от `write_guard.enabled`/`read_guard.enabled`.
- Детерминированный command-scan — **tripwire, не гарантия**; honest known-limitation в коде и доке.
- Привилегированные writers (память/`/init`/онбординг) пишут в обход guard.
- Не ослаблять security: неузнанное → fail-closed. Коммиты лаконичные, без AI-атрибуции.

---

## File Structure

- Modify: `src/security/protected_paths.py` — + `classify_path()`, `protected_decision()`, `protection_enabled()`.
- Modify: `src/security/path_policy.py` — карв-аут в write-flow (`enforce_write_guard`).
- Modify: `src/security/read_guard.py` — инжект operational→denied, skills→allowed.
- Create: `src/security/command_guard.py` — детерминированный tripwire для `execute_command`/`run_python_code`.
- Modify: `src/security/command_moderation.py` — инжект protected-путей в промпт судьи; C1 Часть 2 (judge-broke → confirm-порт).
- Modify: `src/config_modules/security.py` — схема `protected_paths` (enabled/extra).
- Modify: `src/project_memory/storage.py` + `src/tools/project/project_memory.py` — бэкап в `gc_state` + авто-restore.
- Modify: `config.default.yml`, `demo_config.yml` — блок `security.protected_paths`.

---

## Task 1: Классификация пути → решение (extend `protected_paths.py`)

**Files:**
- Modify: `src/security/protected_paths.py`
- Test: `tests/security/test_protected_decision.py`

**Interfaces:**
- Consumes: `protected_paths()`, `ProtectedSet` (План A Task 4).
- Produces:
  - `def classify_path(path: str | Path, ps: ProtectedSet) -> str` → `"skills" | "instructions" | "operational" | "none"` (skills-исключение имеет приоритет над operational).
  - `def protected_decision(path, *, cwd=None, extra=None) -> str` → `"allow" | "ask" | "deny"` (skills/none→allow, instructions→ask, operational→deny).
  - `def protection_enabled(config_dict: dict) -> bool` → дефолт True; False только при явном `security.protected_paths.enabled: false`.

Сопоставление путей — «кандидат внутри защищаемого корня» через существующий `is_within_root` ([path_policy.py](../../src/security/path_policy.py)), case-insensitive на Windows.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_protected_decision.py
from security.protected_paths import protected_decision, protection_enabled


def test_decisions(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    proj = tmp_path / "proj"

    assert protected_decision(str(proj / ".gc" / "config.yml"), cwd=str(proj)) == "deny"
    assert protected_decision(str(proj / "AGENTS.md"), cwd=str(proj)) == "ask"
    assert protected_decision(str(proj / ".gc" / "skills" / "foo" / "SKILL.md"), cwd=str(proj)) == "allow"
    assert protected_decision(str(proj / "src" / "main.py"), cwd=str(proj)) == "allow"


def test_protection_enabled_default_true():
    assert protection_enabled({}) is True
    assert protection_enabled({"protected_paths": {"enabled": False}}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_protected_decision.py -v`
Expected: FAIL — `ImportError: protected_decision`.

- [ ] **Step 3: Write minimal implementation**

Добавить в `src/security/protected_paths.py`:

```python
from security.path_policy import is_within_root


def classify_path(path, ps: ProtectedSet) -> str:
    p = os.path.abspath(os.fspath(path))
    ci = os.name == "nt"
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.skills_exceptions):
        return "skills"
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.instructions):
        return "instructions"
    if any(is_within_root(p, r, case_insensitive=ci) for r in ps.operational):
        return "operational"
    return "none"


def protected_decision(path, *, cwd=None, extra=None) -> str:
    cls = classify_path(path, protected_paths(cwd=cwd, extra=extra))
    if cls == "instructions":
        return "ask"
    if cls == "operational":
        return "deny"
    return "allow"


def protection_enabled(config_dict: dict) -> bool:
    block = (config_dict or {}).get("protected_paths") or {}
    return bool(block.get("enabled", True))
```

(Примечание: для `instructions`-файлов `classify_path` должен matchиться по самому файлу; `is_within_root` для файла-как-корня вернёт True только на точное совпадение — это нужное поведение, т.к. в `ps.instructions` лежат конкретные файлы, не каталоги.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/security/test_protected_decision.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/security/protected_paths.py tests/security/test_protected_decision.py
git commit -m "feat(security): классификация пути -> решение (deny/ask/allow) + protection_enabled"
```

---

## Task 2: Карв-аут в `write_guard`

**Files:**
- Modify: `src/security/path_policy.py` (`enforce_write_guard` wrapper)
- Test: `tests/security/test_write_guard_protected.py`

**Interfaces:**
- Consumes: `protected_decision`, `protection_enabled` (Task 1); `request_confirm` (План A Task 5).
- Produces: до обычной perimeter-проверки `enforce_write_guard` консультирует protected-floor: `deny`→блок; `ask`→confirm-порт (отказ → блок); `allow`→дальше как сейчас. Применяется, только если `protection_enabled(config)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_write_guard_protected.py
import json, types
from security.path_policy import enforce_write_guard


class _Tool:
    def __init__(self, ctx):
        self._ctx = ctx
    @enforce_write_guard(path_arg="path")
    def write(self, path, content):
        return json.dumps({"ok": True})


def _ctx(cwd, confirm=False):
    return types.SimpleNamespace(
        config_dict={"launch_root": str(cwd), "write_guard": {"enabled": True, "mode": "deny"}},
        is_tui=False, tui_confirm=None, logger=None,
    )


def test_operational_gc_write_denied(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = _ctx(tmp_path)
    res = _Tool(ctx).write(path=str(tmp_path / ".gc" / "config.yml"), content="x")
    assert json.loads(res)["ok"] is False  # заблокировано floor-ом


def test_skills_write_allowed(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = _ctx(tmp_path)
    (tmp_path / ".gc" / "skills" / "foo").mkdir(parents=True)
    res = _Tool(ctx).write(path=str(tmp_path / ".gc" / "skills" / "foo" / "SKILL.md"), content="x")
    assert json.loads(res)["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_write_guard_protected.py -v`
Expected: FAIL — операционная `.gc` сейчас разрешена (в периметре), `ok=True`.

- [ ] **Step 3: Write minimal implementation**

В `enforce_write_guard.wrapper` ([path_policy.py](../../src/security/path_policy.py)) — перед существующей perimeter-логикой добавить floor-консультацию (после извлечения `candidate_path`):

```python
            from security.protected_paths import protected_decision, protection_enabled
            from security.confirm_port import request_confirm
            cfg = context.ctx.config_dict if getattr(context, "ctx", None) else {}
            if protection_enabled(cfg):
                cwd = context.launch_root
                decision = protected_decision(candidate_path, cwd=cwd)
                if decision == "deny":
                    err = {"type": "protected_path_denied", "path": candidate_path,
                           "message": "Path is protected (operational .gc / config)."}
                    audit_policy_decision(context, tool_name=_tool_name(func, args),
                                          decision="blocked", reason_code="protected_deny")
                    return _blocked_result(func, err, args, kwargs, blocked_result_factory=blocked_result_factory)
                if decision == "ask":
                    ok = request_confirm(title="Write guard", question=f"Allow write to {candidate_path}?",
                                         fields=[("path", candidate_path)], ctx=context.ctx)
                    if not ok:
                        err = {"type": "protected_path_denied", "path": candidate_path,
                               "message": "Write to instruction file not confirmed."}
                        return _blocked_result(func, err, args, kwargs, blocked_result_factory=blocked_result_factory)
                    # подтверждено → пропускаем floor, дальше обычная perimeter-проверка
```

(`candidate_path` извлекается тем же `_extract_named_value`, что уже есть в wrapper; floor-блок ставится сразу после него, до `check_write_path_allowed`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/security/test_write_guard_protected.py -v`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `pytest tests/security/ -q`
Expected: без новых падений (существующие write_guard-тесты зелёные).

- [ ] **Step 6: Commit**

```bash
git add src/security/path_policy.py tests/security/test_write_guard_protected.py
git commit -m "feat(security): write_guard floor-карв-аут (operational deny, instructions ask, skills allow)"
```

---

## Task 3: Карв-аут в `read_guard`

**Files:**
- Modify: `src/security/read_guard.py` (`enforce_read_guard.wrapper` — инжект)
- Test: `tests/security/test_read_guard_protected.py`

**Interfaces:**
- Consumes: `protected_paths`, `protection_enabled` (Task 1).
- Produces: read-floor применяется **даже при `read_guard.enabled: false`**: операционная `.gc` → deny; `.gc/skills` → allow; инструкции → allow. Реализация — отдельная floor-проверка перед обычным read_guard-flow.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_read_guard_protected.py
import json, types
from security.read_guard import enforce_read_guard


class _Tool:
    def __init__(self, ctx): self._ctx = ctx
    @enforce_read_guard(path_arg="path")
    def read(self, path): return json.dumps({"ok": True, "data": "secret"})


def _ctx(cwd):
    return types.SimpleNamespace(
        config_dict={"launch_root": str(cwd), "read_guard": {"enabled": False}},
        ctx=None, logger=None)


def test_operational_gc_read_denied_even_when_readguard_off(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    ctx = types.SimpleNamespace(config_dict={"launch_root": str(tmp_path), "read_guard": {"enabled": False}},
                                logger=None)
    res = _Tool(ctx).read(path=str(tmp_path / ".gc" / "config.yml"))
    assert json.loads(res)["ok"] is False


def test_skills_read_allowed(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    (tmp_path / ".gc" / "skills" / "foo").mkdir(parents=True)
    (tmp_path / ".gc" / "skills" / "foo" / "SKILL.md").write_text("x", encoding="utf-8")
    ctx = types.SimpleNamespace(config_dict={"launch_root": str(tmp_path), "read_guard": {"enabled": False}},
                                logger=None)
    res = _Tool(ctx).read(path=str(tmp_path / ".gc" / "skills" / "foo" / "SKILL.md"))
    assert json.loads(res)["ok"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_read_guard_protected.py -v`
Expected: FAIL — при `read_guard.enabled:false` сейчас читается всё.

- [ ] **Step 3: Write minimal implementation**

В `enforce_read_guard.wrapper` ([read_guard.py](../../src/security/read_guard.py)) — в самом начале (до проверки `cfg.enabled`) добавить floor:

```python
            from security.protected_paths import classify_path, protected_paths, protection_enabled
            cfg_dict = _config_from_context(context)
            if protection_enabled(cfg_dict):
                try:
                    cand = _extract_named_value(func, args, kwargs, path_arg, path_extractor, "path")
                    ps = protected_paths(cwd=context.launch_root)
                    if classify_path(cand, ps) == "operational":
                        err = {"type": "protected_path_denied", "path": cand, "operation": "read",
                               "message": "Operational .gc is read-protected."}
                        audit_policy_decision(context, tool_name=_tool_name(func, args),
                                              decision="blocked", reason_code="protected_read_deny")
                        return _read_guard_blocked_result(func, err, args, kwargs, blocked_result_factory=blocked_result_factory)
                except ValueError:
                    pass
```

(skills/instructions/none → floor пропускает, дальше обычный read_guard-flow.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/security/test_read_guard_protected.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/security/read_guard.py tests/security/test_read_guard_protected.py
git commit -m "feat(security): read_guard floor — operational .gc deny (даже при read_guard off), skills allow"
```

---

## Task 4: Command-guard tripwire (новый детерминированный слой)

**Files:**
- Create: `src/security/command_guard.py`
- Test: `tests/security/test_command_guard.py`

**Interfaces:**
- Consumes: `protected_paths`, `classify_path`, `protection_enabled` (Task 1).
- Produces:
  - `def scan_command_for_protected(command: str, *, cwd: str, extra=None) -> str` → `"deny" | "ask" | "allow"`: ищет в строке команды токены-пути, классифицирует; operational→deny, instructions→ask, skills/none→allow. `skills`-токен перекрывает operational в той же команде только для самого skills-пути.
  - Декоратор/хелпер для подключения к `execute_command`/`run_python_code` (тонкая проводка, как у write_guard).

Эвристика токенизации: разбить по пробелам/кавычкам, нормализовать относительные к `cwd`, проверить и явные подстроки `.gc` (на случай `rm -rf .gc`). Честный known-limitation: обфускация не ловится.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_command_guard.py
from security.command_guard import scan_command_for_protected


def test_rm_rf_gc_denied(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert scan_command_for_protected("rm -rf .gc && git clone x .", cwd=str(tmp_path)) == "deny"


def test_skills_path_allowed(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert scan_command_for_protected("python .gc/skills/foo/scripts/x.py", cwd=str(tmp_path)) == "allow"


def test_plain_command_allowed(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    assert scan_command_for_protected("mkdir build && pytest", cwd=str(tmp_path)) == "allow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_command_guard.py -v`
Expected: FAIL — `ModuleNotFoundError: security.command_guard`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/security/command_guard.py
"""Детерминированный tripwire для shell/python-команд: ловит ОЧЕВИДНЫЕ
обращения к protected-путям (rm -rf .gc и т.п.). Это НЕ гарантия — обфускация
не ловится; второй рубеж — LLM-судья (command_moderation), страховка — бэкап
памяти (Спека B §7)."""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from security.protected_paths import classify_path, protected_paths

_TOKEN_RE = re.compile(r"[\s=;&|()<>]+")


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return [t for t in _TOKEN_RE.split(command) if t]


def scan_command_for_protected(command: str, *, cwd: str, extra=None) -> str:
    ps = protected_paths(cwd=cwd, extra=extra)
    worst = "allow"
    rank = {"allow": 0, "ask": 1, "deny": 2}
    for tok in _tokens(command):
        cand = tok if os.path.isabs(tok) else os.path.join(cwd, tok)
        cls = classify_path(cand, ps)
        decision = {"skills": "allow", "none": "allow",
                    "instructions": "ask", "operational": "deny"}[cls]
        if rank[decision] > rank[worst]:
            worst = decision
    return worst
```

Проводка (тонкая, без отдельного теста): подключить `scan_command_for_protected` в `execute_command`/`run_python_code` перед запуском — `deny`→блок, `ask`→`request_confirm` (отказ→блок), при `protection_enabled(config)`. Tripwire идёт **до** судьи `command_moderation`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/security/test_command_guard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/security/command_guard.py tests/security/test_command_guard.py
git commit -m "feat(security): command-guard tripwire (detect protected-пути в команде)"
```

---

## Task 5: Инжект protected-путей в промпт судьи + C1 Часть 2

**Files:**
- Modify: `src/security/command_moderation.py` (`build_judge_payload` / промпт; `decide_command_execution` — judge-broke → confirm-порт)
- Test: `tests/security/test_command_moderation_protected.py`

**Interfaces:**
- Consumes: `protected_paths` (список путей для промпта), `request_confirm`, `confirm_available` (План A Task 5).
- Produces:
  - В payload судьи добавлен список protected-путей с правилом «эти пути трогать/удалять нельзя».
  - `decide_command_execution`: при `judge_unavailable` и `mode != allow` → если confirm доступен → ask через порт; иначе → fail-closed deny (как сейчас). Расширяет существующую ask-ветку на `deny`-режим.

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_command_moderation_protected.py
from security.command_moderation import decide_command_execution


def test_judge_unavailable_deny_mode_asks_when_confirm_available(monkeypatch):
    monkeypatch.setattr("security.command_moderation.confirm_available", lambda ctx: True)
    monkeypatch.setattr("security.command_moderation.request_confirm", lambda **k: True)
    dec = decide_command_execution("deny", judge_result=None, judge_unavailable=True, ctx=object())
    assert dec.allowed is True  # пользователь подтвердил


def test_judge_unavailable_deny_mode_blocks_when_no_confirm(monkeypatch):
    monkeypatch.setattr("security.command_moderation.confirm_available", lambda ctx: False)
    dec = decide_command_execution("deny", judge_result=None, judge_unavailable=True, ctx=object())
    assert dec.allowed is False  # fail-closed
```

(Сигнатуру `decide_command_execution` уточнить по факту кода — сейчас она принимает `mode` + флаги; добавить параметр `ctx` и ветку confirm-порта. Имя поля результата (`allowed`) — сверить с `JudgeDecision`/`reason_code` в модуле и привести тест к реальному.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_command_moderation_protected.py -v`
Expected: FAIL — текущая `decide_command_execution` при `deny`+unavailable безусловно блокирует.

- [ ] **Step 3: Write minimal implementation**

В `decide_command_execution` ([command_moderation.py:168](../../src/security/command_moderation.py)) ветку «judge unavailable» обобщить: вместо «ask только при mode==ask, иначе deny» — при `mode != "allow"` сначала пробовать confirm-порт:

```python
from security.confirm_port import confirm_available, request_confirm
# ...
    if judge_unavailable and normalized_mode != "allow":
        if confirm_available(ctx) and request_confirm(
            title="Command moderation",
            question="Судья недоступен. Выполнить команду?",
            fields=[("mode", normalized_mode)],
            ctx=ctx,
        ):
            return JudgeDecision(allowed=True, reason_code="judge_unavailable_user_confirmed",
                                 reason="User confirmed execution after judge was unavailable.")
        return JudgeDecision(allowed=False, reason_code="judge_unavailable",
                             reason="Judge is unavailable; fail-closed.")
```

В `build_judge_payload` добавить в промпт строку с protected-путями (из `protected_paths(cwd=...)`): «Never touch/delete: <paths>». (C1 Часть 1 — нормализация/ретрай — уже в коде, не трогать.)

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/security/test_command_moderation_protected.py -v`
Expected: PASS.

- [ ] **Step 5: Regression**

Run: `pytest tests/security/test_command_judge_robustness.py tests/security/ -q`
Expected: без новых падений (C1 Часть 1 зелёная).

- [ ] **Step 6: Commit**

```bash
git add src/security/command_moderation.py tests/security/test_command_moderation_protected.py
git commit -m "feat(security): судья — инжект protected-путей + C1 Часть 2 (broke -> confirm-порт)"
```

---

## Task 6: Floor-конфиг `security.protected_paths`

**Files:**
- Modify: `src/config_modules/security.py` (+ модель `ProtectedPathsConfig`)
- Modify: `config.default.yml`, `demo_config.yml`
- Test: `tests/security/test_protected_paths_config.py`

**Interfaces:**
- Produces: `class ProtectedPathsConfig(BaseModel): enabled: bool = True; extra: list[str] = []` в `SecurityConfig`. Парсится из `security.protected_paths`. `extra` прокидывается в `protected_paths(extra=...)` потребителями (где доступен cwd).

- [ ] **Step 1: Write the failing test**

```python
# tests/security/test_protected_paths_config.py
from config_modules.security import SecurityConfig


def test_defaults_enabled_true():
    cfg = SecurityConfig()
    assert cfg.protected_paths.enabled is True
    assert cfg.protected_paths.extra == []


def test_parse_disabled():
    cfg = SecurityConfig(protected_paths={"enabled": False, "extra": ["/x"]})
    assert cfg.protected_paths.enabled is False
    assert cfg.protected_paths.extra == ["/x"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/security/test_protected_paths_config.py -v`
Expected: FAIL — `SecurityConfig` не имеет `protected_paths`.

- [ ] **Step 3: Write minimal implementation**

В `src/config_modules/security.py`:

```python
class ProtectedPathsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    extra: list[str] = Field(default_factory=list)
```

Добавить поле в `SecurityConfig`:

```python
    protected_paths: ProtectedPathsConfig = Field(default_factory=ProtectedPathsConfig)
```

В `config.default.yml` (секция `security`) и `demo_config.yml` добавить:

```yaml
  protected_paths:
    enabled: true
    extra: []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/security/test_protected_paths_config.py -v`
Expected: PASS.

- [ ] **Step 5: Regression конфига**

Run: `pytest tests/ -k "config or onboarding or security" -q`
Expected: без новых падений (синхрон `config.default.yml`/`demo_config.yml` — см. правило проекта).

- [ ] **Step 6: Commit**

```bash
git add src/config_modules/security.py config.default.yml demo_config.yml tests/security/test_protected_paths_config.py
git commit -m "feat(config): security.protected_paths (enabled/extra), on по умолчанию"
```

---

## Task 7: Бэкап long memory в `gc_state` + авто-restore

**Files:**
- Modify: `src/project_memory/storage.py` (+ зеркалирование/restore по второму пути)
- Modify: `src/tools/project/project_memory.py` (передать backup-путь)
- Test: `tests/project_memory/test_memory_backup.py`

**Interfaces:**
- Consumes: `env_vars.gc_paths()["state"]`, `env_vars.project_slug()`.
- Produces:
  - `def backup_path(*, cwd=None) -> Path` → `<gc_state>/projects/<slug>/GIGACODE_memory_backup.md`.
  - В `write`/`append` storage пишет и в основной путь (`.gc/`), и в backup (атомарно). В `read`/`stat`, если основной отсутствует, но backup есть → восстановить основной из backup и вернуть его.

- [ ] **Step 1: Write the failing test**

```python
# tests/project_memory/test_memory_backup.py
from pathlib import Path
from project_memory import storage


def test_write_mirrors_to_backup(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    main = tmp_path / ".gc" / "GIGACODE_memory.md"
    storage.write(main, "FACTS", backup=storage.backup_path(cwd=str(tmp_path)))
    assert storage.backup_path(cwd=str(tmp_path)).read_text(encoding="utf-8") == "FACTS"


def test_read_restores_from_backup_when_main_deleted(tmp_path, monkeypatch):
    from config_modules import env_vars
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc")
    monkeypatch.chdir(tmp_path)
    main = tmp_path / ".gc" / "GIGACODE_memory.md"
    bkp = storage.backup_path(cwd=str(tmp_path))
    storage.write(main, "FACTS", backup=bkp)
    main.unlink()  # симулируем rm -rf .gc
    text = storage.read(main, backup=bkp)
    assert text == "FACTS"
    assert main.exists()  # восстановлен
```

(Сигнатуры `storage.write/read` — расширить опциональным `backup=` без слома существующих вызовов; имена сверить с реальным `storage.py`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/project_memory/test_memory_backup.py -v`
Expected: FAIL — нет `backup_path`/`backup`-параметра.

- [ ] **Step 3: Write minimal implementation**

В `src/project_memory/storage.py` добавить `backup_path()` и опциональное зеркалирование:

```python
from config_modules.env_vars import gc_paths, project_slug


def backup_path(*, cwd=None) -> Path:
    slug = project_slug(cwd) if cwd else project_slug()
    return gc_paths()["state"] / "projects" / slug / "GIGACODE_memory_backup.md"
```

В `write`/`append` после атомарной записи основного — записать тем же содержимым в `backup` (если передан, `mkdir parents`). В `read`/`stat` — если основной отсутствует, а `backup` есть, скопировать backup→основной и читать основной.

В `src/tools/project/project_memory.py` прокинуть `backup=backup_path(cwd=...)` в вызовы storage (путь основного файла берётся из `ctx.project_gc_dir`, как сейчас).

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/project_memory/test_memory_backup.py -v`
Expected: PASS.

- [ ] **Step 5: Regression памяти**

Run: `pytest tests/ -k "memory" -q`
Expected: без новых падений.

- [ ] **Step 6: Commit**

```bash
git add src/project_memory/storage.py src/tools/project/project_memory.py tests/project_memory/test_memory_backup.py
git commit -m "feat(memory): бэкап long memory в gc_state + авто-restore при пропаже .gc"
```

---

## Self-Review (выполнено при написании)

- **Покрытие спеки:** §4 классы путей → Task 1; §5.1 write карв-аут → Task 2; §5.2 read карв-аут → Task 3; §5.3 command-guard (tripwire) → Task 4, (judge-инжект + C1) → Task 5; §5.4 привилегированные writers → обеспечено тем, что память/`/init` пишут своими путями (Task 7 / План A Task 8), не через гард-декораторы; §6 floor+opt-out → Task 1 (`protection_enabled`) + Task 6 (конфиг); §7 бэкап памяти → Task 7; §9 known limitations → зафиксированы в docstring `command_guard.py` (Task 4).
- **Floor независим от тумблеров гардов:** Task 2 (write) и Task 3 (read) консультируют floor отдельно от `write_guard.enabled`/`read_guard.enabled`; Task 3-тест прямо проверяет работу при `read_guard.enabled: false`.
- **Типы:** `protected_paths/ProtectedSet` (План A) → `classify_path/protected_decision/protection_enabled` (Task 1) → потребители (Task 2/3/4/5). `scan_command_for_protected` (Task 4), `backup_path` (Task 7) — согласованы.
- **Сверка с кодом — отметки для исполнителя:** точные сигнатуры `decide_command_execution`/`JudgeDecision` (Task 5) и `storage.write/read/append` (Task 7) уточнить по факту модулей и привести тесты к реальным именам полей; это помечено в задачах.
- **Зависимость:** весь План B требует внедрённого Плана A (фундамент `protected_paths`/`confirm_port`).
