# Agent Trace Robustness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить зацикленные tool-call ошибки и сделать трейс агента в TUI честным: невалидные вызовы финализируются (не висят «running»), терминальный статус отражает реальный исход, аргументы с двойным кодированием чинятся, skill_id-резолв подсказывает кандидатов.

**Architecture:** 4 независимых фикса по слоям-владельцам. 3b/3a — чистый харнес (валидация аргументов / skills-registry). №1 — харнес (проброс `call_id` в technical-feedback) + TUI (mapper/render). №2 — поле `State` + мост через app-флаг + новые состояния индикатора TUI. Возврат `run_agent` и формат сообщений не ломаем — всё аддитивно.

**Tech Stack:** Python ≥3.10, pytest (conftest кладёт `src/` в sys.path; абсолютные импорты от `src/`), jsonschema (Draft7), Textual (TUI).

## Global Constraints

- Импорты — абсолютные от `src/` (`from tools.skills.registry import ...`), не относительные, не `from src...`.
- TDD: сначала падающий тест, потом минимальная реализация.
- Тесты mapper/валидации — на реальном формате payload, не на гипотетических моках.
- TUI-тесты запускать через `uv run pytest ...` (extra `[dev-tui]` только в `.venv/`).
- Коммиты — лаконичные, на русском, без AI-атрибуции и упоминаний Claude/Anthropic. Хуки не пропускать (`--no-verify` запрещён без явной просьбы).
- Линтера в проекте нет — вслепую не запускать.
- Новых конфиг-ключей план не вводит → синхронизация `demo_config.yml`/`config.default.yml` не требуется.

---

## Группа A — Фикс 3b: коэрция двойного кодирования + честный фидбэк

Самодостаточно, только `src/tools/arguments_validation.py`. Снимает главный триггер циклов.

### Task A1: Коэрция двойного кодирования аргументов

**Files:**
- Modify: `src/tools/arguments_validation.py` (импорт + новый хелпер `_coerce_double_encoded_fields`; блок `except` в `parse_and_validate_tool_arguments:194-204`)
- Test: `tests/test_arguments_validation_coercion.py`

**Interfaces:**
- Produces: `_coerce_double_encoded_fields(parsed_args: dict, params_schema: dict) -> tuple[dict, bool]` — `(coerced_or_original, changed)`.
- Consumes: существующий `parse_and_validate_tool_arguments(*, tool_name, arguments, tool_spec, allow_empty_for_empty_schema, tool_fabric=None) -> dict`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_arguments_validation_coercion.py
from tools.arguments_validation import parse_and_validate_tool_arguments

_SPEC = {
    "parameters": {
        "type": "object",
        "properties": {
            "summary": {"type": "string"},
            # поле-объект через anyOf без строковой ветки (как mcp ...testUnit.create.attributes)
            "attributes": {"anyOf": [{"type": "object"}, {"type": "null"}]},
        },
        "required": ["summary"],
        "additionalProperties": False,
    }
}

def test_double_encoded_object_is_coerced():
    # attributes пришёл JSON-строкой второго уровня
    args = '{"summary": "TC1", "attributes": "{\\"test_level\\": \\"UI\\"}"}'
    out = parse_and_validate_tool_arguments(
        tool_name="t", arguments=args, tool_spec=_SPEC,
        allow_empty_for_empty_schema=False,
    )
    assert out["attributes"] == {"test_level": "UI"}  # развёрнут в объект

def test_string_valid_as_is_not_touched():
    spec = {"parameters": {"type": "object",
            "properties": {"x": {"anyOf": [{"type": "string"}, {"type": "object"}]}},
            "required": [], "additionalProperties": False}}
    out = parse_and_validate_tool_arguments(
        tool_name="t", arguments='{"x": "123"}', tool_spec=spec,
        allow_empty_for_empty_schema=False,
    )
    assert out["x"] == "123"  # строка валидна по строковой ветке → НЕ коэрсим в 123

def test_unrecoverable_still_rejected():
    import pytest
    from tools.arguments_validation import WrongFunctionArgumentsException
    # строка, чей json.loads даёт скаляр (не dict/list) → коэрция не применяется
    args = '{"summary": "TC1", "attributes": "42"}'
    with pytest.raises(WrongFunctionArgumentsException):
        parse_and_validate_tool_arguments(
            tool_name="t", arguments=args, tool_spec=_SPEC,
            allow_empty_for_empty_schema=False,
        )
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `pytest tests/test_arguments_validation_coercion.py -v`
Expected: `test_double_encoded_object_is_coerced` FAIL (WrongFunctionArgumentsException — коэрции ещё нет).

- [ ] **Step 3: Добавить импорт и хелпер коэрции**

В шапке файла (после `from tools.connect_mcp.validation import _validate_tool_arguments`):

```python
from jsonschema import Draft7Validator
```

Перед `parse_and_validate_tool_arguments` добавить:

```python
def _coerce_double_encoded_fields(
    parsed_args: Dict[str, Any], params_schema: Dict[str, Any]
) -> tuple[Dict[str, Any], bool]:
    """Recovery-проход: развернуть поля, пришедшие JSON-строкой второго уровня.

    Для каждого строкового значения, которое не проходит свою под-схему, пробуем
    json.loads; принимаем только dict/list, который под-схему проходит. Строки,
    валидные как есть, не трогаем (поэтому "123" не превращается в 42). Возвращаем
    (изменённая_копия_или_исходник, changed)."""
    properties = params_schema.get("properties", {}) or {}
    if not isinstance(parsed_args, dict) or not properties:
        return parsed_args, False
    coerced = dict(parsed_args)
    changed = False
    for field, value in parsed_args.items():
        if not isinstance(value, str):
            continue
        subschema = properties.get(field)
        if not isinstance(subschema, dict):
            continue
        if Draft7Validator(subschema).is_valid(value):
            continue  # строка валидна как есть — не трогаем
        try:
            decoded = json.loads(value)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(decoded, (dict, list)):
            continue
        if Draft7Validator(subschema).is_valid(decoded):
            coerced[field] = decoded
            changed = True
    return coerced, changed
```

- [ ] **Step 4: Встроить recovery в `except` блок**

Заменить блок `parse_and_validate_tool_arguments:194-204`:

```python
    try:
        _validate_tool_arguments(tool_name, params_schema, parsed_args)
    except Exception as e:  # noqa: BLE001
        coerced, changed = _coerce_double_encoded_fields(parsed_args, params_schema)
        recovered = False
        if changed:
            try:
                _validate_tool_arguments(tool_name, params_schema, coerced)
            except Exception:  # noqa: BLE001
                recovered = False
            else:
                parsed_args = coerced
                recovered = True
        if not recovered:
            _raise_wrong_function_arguments(
                message=str(e),
                received_args=json.dumps(parsed_args, ensure_ascii=False),
                expected_schema=tool_spec,
                tool_name=tool_name,
                tool_fabric=tool_fabric,
                parsed_args=parsed_args,
            )

    return normalize_optional_empty_as_omit(parsed_args, params_schema)
```

- [ ] **Step 5: Прогнать — убедиться, что зелёные**

Run: `pytest tests/test_arguments_validation_coercion.py -v`
Expected: 3 PASS.

- [ ] **Step 6: Регресс существующих тестов валидации**

Run: `pytest tests/ -k "argument or validation or tool" -q`
Expected: без новых падений (сверить с baseline пре-существующих Windows-падений).

- [ ] **Step 7: Коммит**

```bash
git add src/tools/arguments_validation.py tests/test_arguments_validation_coercion.py
git commit -m "fix(tools): коэрция двойного кодирования аргументов (строка→объект) на ветке провала валидации"
```

### Task A2: Честный фидбэк — раскрытие anyOf/oneOf вместо `<any>`

**Files:**
- Modify: `src/tools/arguments_validation.py` (хелпер `_describe_schema_type` + замена `arguments_validation.py:78`)
- Test: `tests/test_arguments_validation_feedback.py`

**Interfaces:**
- Produces: `_describe_schema_type(spec: dict) -> str`.
- Consumes: `WrongFunctionArgumentsException.to_llm_feedback() -> str` (существующий).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_arguments_validation_feedback.py
from tools.arguments_validation import WrongFunctionArgumentsException

def test_anyof_object_shown_as_object_not_any():
    schema = {"parameters": {"type": "object", "properties": {
        "attributes": {"anyOf": [{"type": "object"}, {"type": "null"}]},
    }, "required": [], "additionalProperties": False}}
    exc = WrongFunctionArgumentsException(
        message="bad", received_args="{}", expected_schema=schema,
        tool_name="t",
    )
    fb = exc.to_llm_feedback()
    assert '"attributes": <object>' in fb
    assert '"attributes": <any>' not in fb
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `pytest tests/test_arguments_validation_feedback.py -v`
Expected: FAIL (сейчас рендерится `<any>`).

- [ ] **Step 3: Добавить хелпер описания типа**

Перед классом `WrongFunctionArgumentsException`:

```python
def _describe_schema_type(spec: Dict[str, Any]) -> str:
    """Человекочитаемый тип поля для 'Correct call format'.
    Раскрывает anyOf/oneOf (object|array|...), а не наивный <any>."""
    if not isinstance(spec, dict):
        return "any"
    t = spec.get("type")
    if isinstance(t, str):
        return t
    if isinstance(t, list):
        return "|".join(str(x) for x in t if x != "null") or "any"
    for key in ("anyOf", "oneOf"):
        branches = spec.get(key)
        if isinstance(branches, list):
            types: list[str] = []
            for b in branches:
                if isinstance(b, dict):
                    bt = b.get("type")
                    if isinstance(bt, str) and bt != "null" and bt not in types:
                        types.append(bt)
            if types:
                return "|".join(types)
    return "any"
```

- [ ] **Step 4: Заменить наивное извлечение типа**

`arguments_validation.py:78` — заменить:

```python
                ftype = spec.get("type", "any")
```

на:

```python
                ftype = _describe_schema_type(spec)
```

- [ ] **Step 5: Прогнать — зелёный**

Run: `pytest tests/test_arguments_validation_feedback.py -v`
Expected: PASS.

- [ ] **Step 6: Коммит**

```bash
git add src/tools/arguments_validation.py tests/test_arguments_validation_feedback.py
git commit -m "fix(tools): фидбэк по аргументам раскрывает anyOf/oneOf (object) вместо <any>"
```

---

## Группа B — Фикс 3a: lenient-резолв skill_id + кандидаты

Только `src/tools/skills/registry.py`.

### Task B1: Резолв удвоённого namespace + кандидаты в not-found

**Files:**
- Modify: `src/tools/skills/registry.py` (хелпер `_candidate_ids_for` + ветка `:` в `_lookup_skill:524-533` + not-found на `551`)
- Test: `tests/test_skills_registry_lookup.py`

**Interfaces:**
- Consumes: `SkillsRegistry._lookup_skill(skill_id) -> Skill` (приватный), `self._skills_by_id: dict[str, Skill]`.
- Produces: `SkillsRegistry._candidate_ids_for(skill_id: str) -> list[str]`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_skills_registry_lookup.py
import pytest
from tools.skills.registry import SkillsRegistry

class _Skill:
    def __init__(self, sid): self.id = sid

def _registry(ids):
    r = SkillsRegistry.__new__(SkillsRegistry)
    r._skills_by_id = {sid: _Skill(sid) for sid in ids}
    r._ensure_loaded = lambda: None          # тест не грузит ФС
    r.reload = lambda: None
    r._resolve_pack_bootstrap = lambda s: None
    return r

def test_doubled_namespace_resolves_to_flat():
    r = _registry(["qa-tc-generation"])
    skill = r._lookup_skill("qa-tc-generation:qa-tc-generation")
    assert skill.id == "qa-tc-generation"

def test_not_found_lists_candidates():
    r = _registry(["qa-tc-generation"])
    with pytest.raises(KeyError) as ei:
        r._lookup_skill("pack:qa-tc-generatio")  # опечатка хвоста
    assert "qa-tc-generation" in str(ei.value)  # подсказка-кандидат
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `pytest tests/test_skills_registry_lookup.py -v`
Expected: оба FAIL (резолва/кандидатов ещё нет).

- [ ] **Step 3: Добавить хелпер кандидатов**

В классе `SkillsRegistry` (рядом с `_lookup_skill`):

```python
    def _candidate_ids_for(self, skill_id: str) -> List[str]:
        """Кандидаты по совпадению хвоста — для подсказки в not-found ошибке."""
        target = skill_id.split(":")[-1].replace("_", "-")
        cands: List[str] = []
        for canonical in self._skills_by_id:
            tail = canonical.split(":", 1)[1] if ":" in canonical else canonical
            if target and (target in tail or tail in target):
                cands.append(canonical)
        return sorted(cands)

    def _not_found_message(self, skill_id: str) -> str:
        cands = self._candidate_ids_for(skill_id)
        base = f"Skill '{skill_id}' not found"
        return f"{base}. Did you mean: {cands}?" if cands else base
```

- [ ] **Step 4: Дополнить ветку `:` и not-found**

В `_lookup_skill` заменить ветку `if ":" in skill_id:` (524-533):

```python
        # 1) Прямой canonical lookup (если содержит ':').
        if ":" in skill_id:
            skill = self._skills_by_id.get(skill_id)
            if skill is None:
                # Нормализация только skill-сегмента (часть после ':').
                pack_part, _, tail = skill_id.partition(":")
                normalized_tail = tail.replace("_", "-")
                skill = self._skills_by_id.get(f"{pack_part}:{normalized_tail}")
                if skill is None and pack_part in (tail, normalized_tail):
                    # Удвоённый namespace (pack==skill, напр. 'x:x') — модель угадала
                    # форму pack:skill для flat-скилла. Безопасный bare-lookup по хвосту.
                    skill = (self._skills_by_id.get(tail)
                             or self._skills_by_id.get(normalized_tail))
            if skill is None:
                raise KeyError(self._not_found_message(skill_id))
            return skill
```

Заменить `raise KeyError(f"Skill '{skill_id}' not found")` на `551` на:

```python
            raise KeyError(self._not_found_message(skill_id))
```

- [ ] **Step 5: Прогнать — зелёный**

Run: `pytest tests/test_skills_registry_lookup.py -v`
Expected: оба PASS.

- [ ] **Step 6: Регресс существующих skills-тестов**

Run: `pytest tests/ -k "skill" -q`
Expected: без новых падений.

- [ ] **Step 7: Коммит**

```bash
git add src/tools/skills/registry.py tests/test_skills_registry_lookup.py
git commit -m "fix(skills): резолв удвоённого namespace в skill_id и кандидаты в not-found"
```

---

## Группа C — Фикс №1: проброс call_id → финализация строки-вызова

Харнес (проброс `call_id`) + TUI (mapper + render). 3 таска.

### Task C1: `WrongFunctionArgumentsException` несёт call_id; technical-feedback его передаёт

**Files:**
- Modify: `src/tools/arguments_validation.py` (`WrongFunctionArgumentsException.__init__`)
- Modify: `src/processing_responses.py` (`process_tool_calls:387-393` — аннотировать `call_id`; `_make_technical_feedback_user_message:118-127` — параметр `call_id`; эмиссия `759-764`)
- Test: `tests/test_technical_feedback_call_id.py`

**Interfaces:**
- Produces: `WrongFunctionArgumentsException.call_id: str | None`; `_make_technical_feedback_user_message(content, *, technical_type=..., call_id: str | None = None) -> dict` — добавляет `"call_id"` в dict только если не None.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_technical_feedback_call_id.py
from processing_responses import _make_technical_feedback_user_message
from message_policy.user_message_policy import TECHNICAL_TYPE_ERROR_MESSAGE

def test_call_id_attached_as_sibling_key():
    msg = _make_technical_feedback_user_message(
        "boom", technical_type=TECHNICAL_TYPE_ERROR_MESSAGE, call_id="call_42",
    )
    assert msg["role"] == "user"
    assert msg["call_id"] == "call_42"
    assert msg["content"].startswith('<technical type="error_message">')  # текст не тронут

def test_no_call_id_means_no_key():
    msg = _make_technical_feedback_user_message("boom")
    assert "call_id" not in msg
```

- [ ] **Step 2: Прогнать — падает**

Run: `pytest tests/test_technical_feedback_call_id.py -v`
Expected: FAIL (TypeError: unexpected kwarg `call_id`).

- [ ] **Step 3: Добавить call_id в исключение**

`WrongFunctionArgumentsException.__init__` (`arguments_validation.py:38-52`) — добавить параметр и поле:

```python
    def __init__(
        self,
        message: str,
        received_args: str,
        expected_schema: dict | None = None,
        tool_name: str | None = None,
        extra_hint: str | None = None,
        call_id: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema
        self.tool_name = tool_name
        self.extra_hint = extra_hint
        self.call_id = call_id
```

- [ ] **Step 4: Аннотировать call_id в process_tool_calls**

`processing_responses.py:387-393` — обернуть `apply_tool`:

```python
        try:
            tool_res_raw = apply_tool(
                tool_name=tool_name,
                arguments=tool_arguments,
                ctx=ctx,
                state=state,
                run_agent_func=run_agent_func,
            )
        except WrongFunctionArgumentsException as exc:
            if getattr(exc, "call_id", None) is None:
                exc.call_id = m.get("call_id")
            raise
```

> `WrongFunctionArgumentsException` уже импортирован в `processing_responses.py` (используется в `except` на 756). Если импорта в области `process_tool_calls` нет — добавить к существующему импорту из `tools`.

- [ ] **Step 5: Параметр call_id у фабрики сообщения + эмиссия**

`_make_technical_feedback_user_message` (118-127):

```python
def _make_technical_feedback_user_message(
    content: str, *, technical_type: str = TECHNICAL_TYPE_TECHNICAL,
    call_id: str | None = None,
) -> Dict[str, str]:
    msg = {
        "role": "user",
        "content": wrap_technical_user_content(
            content, technical_type=technical_type
        ),
        "id": generate_message_id_func("m"),
    }
    if call_id:
        msg["call_id"] = call_id
    return msg
```

Эмиссия в `except WrongFunctionArgumentsException` (759-764):

```python
                messages = [
                    _make_technical_feedback_user_message(
                        e.to_llm_feedback(),
                        technical_type=TECHNICAL_TYPE_ERROR_MESSAGE,
                        call_id=getattr(e, "call_id", None),
                    )
                ]
```

- [ ] **Step 6: Прогнать — зелёный**

Run: `pytest tests/test_technical_feedback_call_id.py -v`
Expected: 2 PASS.

- [ ] **Step 7: Коммит**

```bash
git add src/tools/arguments_validation.py src/processing_responses.py tests/test_technical_feedback_call_id.py
git commit -m "fix(harness): technical-feedback по невалидному tool-call несёт call_id отказавшего вызова"
```

### Task C1b: не отправлять call_id user-сообщения на wire в режиме responses

**Контекст:** call_id на technical-feedback нужен TUI/persist, но в `responses`-режиме `create_call_data_responses` кладёт `input = messages` **дословно** (`src/utils/connect_backend.py:327`) — лишний ключ ушёл бы на бэкенд. В `chat_completions` он уже срезается формат-конвертером (`_legacy_role_to_chat_completions_message` строит свежий `{role, content}`), там правка не нужна. Снимаем call_id с role-based message-элементов **не мутируя** исходные dict'ы (persist/TUI сохраняют call_id); на `function_call`/`function_call_output` call_id обязателен — не трогаем.

**Files:**
- Modify: `src/utils/connect_backend.py` (хелпер + `create_call_data_responses:327`)
- Test: `tests/test_responses_input_call_id.py`

**Interfaces:**
- Consumes: `create_call_data_responses(messages, state, tools=None, set_previous_response_id=True) -> dict`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_responses_input_call_id.py
from types import SimpleNamespace
from utils.connect_backend import create_call_data_responses

def test_responses_input_strips_extra_call_id_from_message_items():
    state = SimpleNamespace(default_model="m", tools=[], call_metadata={},
                            previous_response_id=None)
    msgs = [
        {"role": "user", "content": "x", "id": "m1", "call_id": "call_1"},
        {"type": "function_call", "call_id": "call_2", "name": "f", "arguments": "{}"},
    ]
    data = create_call_data_responses(msgs, state)
    assert "call_id" not in data["input"][0]      # снят с user-сообщения
    assert data["input"][1]["call_id"] == "call_2"  # на function_call сохранён
    assert msgs[0]["call_id"] == "call_1"          # исходник НЕ мутирован (TUI/persist)
```

- [ ] **Step 2: Прогнать — падает**

Run: `pytest tests/test_responses_input_call_id.py -v`
Expected: FAIL (`call_id` остаётся в input[0]).

- [ ] **Step 3: Хелпер + применение**

В `src/utils/connect_backend.py` перед `create_call_data_responses`:

```python
def _responses_input_without_message_call_id(messages):
    """call_id на role-based message-элементах (напр. technical-feedback user) —
    только для TUI/persist; в responses input не отправляем. На function_call/
    function_call_output call_id обязателен — сохраняем. Не мутируем исходники."""
    cleaned = []
    for m in messages:
        if (isinstance(m, dict) and "call_id" in m
                and m.get("type") not in ("function_call", "function_call_output")):
            m = {k: v for k, v in m.items() if k != "call_id"}
        cleaned.append(m)
    return cleaned
```

В `create_call_data_responses` заменить `call_data["input"] = messages` (327):

```python
    call_data["input"] = _responses_input_without_message_call_id(messages)
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `pytest tests/test_responses_input_call_id.py -v`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
git add src/utils/connect_backend.py tests/test_responses_input_call_id.py
git commit -m "fix(harness): не слать call_id user-сообщения в responses input (только TUI/persist)"
```

### Task C2: trace_mapper кладёт call_id на error_msg-событие

**Files:**
- Modify: `src/tui/services/trace_mapper.py` (`error_msg` TraceEvent `123-127`)
- Test: `tests/tui/test_trace_mapper_error_msg.py`

**Interfaces:**
- Consumes: `map_message(msg: dict, *, no: int, ts: str) -> TraceEvent | None`; `TraceEvent.call_id` (уже в `src/tui/models.py:56`).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/tui/test_trace_mapper_error_msg.py
from tui.services.trace_mapper import map_message

def test_error_msg_carries_call_id():
    msg = {
        "role": "user",
        "content": '<technical type="error_message">❌ Wrong arguments for `x`</technical>',
        "id": "m1",
        "call_id": "call_99",
    }
    ev = map_message(msg, no=5, ts="2026-06-30T00:00:00Z")
    assert ev.kind == "error_msg"
    assert ev.call_id == "call_99"

def test_error_msg_without_call_id_is_none():
    msg = {"role": "user",
           "content": '<technical type="error_message">boom</technical>', "id": "m2"}
    ev = map_message(msg, no=6, ts="2026-06-30T00:00:00Z")
    assert ev.kind == "error_msg"
    assert ev.call_id is None
```

- [ ] **Step 2: Прогнать — падает**

Run: `uv run pytest tests/tui/test_trace_mapper_error_msg.py -v`
Expected: `test_error_msg_carries_call_id` FAIL (call_id is None).

- [ ] **Step 3: Прокинуть call_id в error_msg-событие**

`trace_mapper.py:123-127` — добавить `call_id`:

```python
                if tech_type == "error_message":
                    # Ошибки бэкенда — заметная красная строка (см. _render_event).
                    cid = msg.get("call_id")
                    return TraceEvent(
                        no=no, event_ts_utc_iso=ts, kind="error_msg",
                        title="!", summary=inner_text,
                        full_payload=msg, message_id=mid,
                        call_id=cid if isinstance(cid, str) else None,
                    )
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `uv run pytest tests/tui/test_trace_mapper_error_msg.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Коммит**

```bash
git add src/tui/services/trace_mapper.py tests/tui/test_trace_mapper_error_msg.py
git commit -m "fix(tui): trace_mapper кладёт call_id на error_msg-событие"
```

### Task C3: render финализирует строку-вызов и подавляет дублирующую `[!]`-строку

**Files:**
- Modify: `src/tui/screens/main.py` (ветка `error_msg` в `_render_event:1074-1079`)
- Test: `tests/tui/test_main_error_msg_finalizes.py`

**Interfaces:**
- Consumes: `self._active_calls: dict[str, ToolCallRow]`; `ToolCallRow.set_result(*, summary, error)` (уже используется на `main.py:1037`).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/tui/test_main_error_msg_finalizes.py
# Юнит на ветку рендера: подменяем _active_calls фейковой строкой и проверяем,
# что error_msg с известным call_id финализирует её, а отдельная [!]-строка не плодится.
from tui.models import TraceEvent
from tui.screens.main import TraceScreen

class _FakeRow:
    def __init__(self): self.finalized = None
    def set_result(self, *, summary, error): self.finalized = (summary, error)

def _ev(call_id):
    return TraceEvent(no=1, event_ts_utc_iso="", kind="error_msg",
                      title="!", summary="Wrong arguments for `x`",
                      full_payload={}, call_id=call_id)

def test_error_msg_with_call_id_finalizes_row():
    screen = TraceScreen.__new__(TraceScreen)
    row = _FakeRow()
    screen._active_calls = {"call_7": row}
    mounted = []
    screen._mount_trace_row = lambda **kw: mounted.append(kw)
    screen._render_error_msg(_ev("call_7"))
    assert row.finalized == ("Wrong arguments for `x`", True)
    assert mounted == []            # [!]-строка подавлена
    assert "call_7" not in screen._active_calls

def test_error_msg_without_match_falls_back():
    screen = TraceScreen.__new__(TraceScreen)
    screen._active_calls = {}
    mounted = []
    screen._mount_trace_row = lambda **kw: mounted.append(kw)
    screen._render_error_msg(_ev(None))
    assert len(mounted) == 1        # fallback [!]-строка
    assert mounted[0]["prefix"] == "[!] "
```

> Реализация выносит ветку `error_msg` в метод `_render_error_msg(ev)` (его зовёт `_render_event`) — это необходимая для теста граница (один кадр TUI без полного pilot), а не «улучшение по месту».

- [ ] **Step 2: Прогнать — падает**

Run: `uv run pytest tests/tui/test_main_error_msg_finalizes.py -v`
Expected: FAIL (`_render_error_msg` нет).

- [ ] **Step 3: Вынести ветку и добавить финализацию**

`main.py:1074-1079` — заменить тело ветки на вызов нового метода:

```python
        if ev.kind == "error_msg":
            self._render_error_msg(ev)
            return
```

Рядом добавить метод:

```python
    def _render_error_msg(self, ev) -> None:
        """error_msg с известным call_id финализирует соответствующую строку-вызов
        (✗ inline, таймер стоп) и подавляет дублирующую [!]-строку. Иначе — [!] как есть."""
        row = self._active_calls.get(ev.call_id) if ev.call_id else None
        if row is not None:
            row.set_result(summary=ev.summary, error=True)
            self._active_calls.pop(ev.call_id, None)
            return
        self._mount_trace_row(
            prefix="[!] ", content=ev.summary,
            kind_class="line-error", event_no=ev.no,
        )
```

- [ ] **Step 4: Прогнать — зелёный**

Run: `uv run pytest tests/tui/test_main_error_msg_finalizes.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Снапшот-регресс TUI**

Run: `uv run pytest tests/tui -q`
Expected: без новых падений (снапшоты флапают в полном прогоне — при сомнении гонять подозрительный тест в изоляции).

- [ ] **Step 6: Коммит**

```bash
git add src/tui/screens/main.py tests/tui/test_main_error_msg_finalizes.py
git commit -m "fix(tui): error_msg с call_id финализирует строку-вызов и подавляет дублирующую [!]-строку"
```

---

## Группа D — Фикс №2: честный терминальный статус

Поле `State` + мост (executor→runner→app) + новые состояния индикатора + ветка спадающего фронта. 4 таска.

### Task D1: `State.terminal_outcome` + установка в точках терминирования

**Files:**
- Modify: `src/states.py` (поле рядом с `finish_gate_rejection_count:88`)
- Modify: `src/processing_responses.py` (`541-545`)
- Test: `tests/test_terminal_outcome.py`

**Interfaces:**
- Produces: `State.terminal_outcome: str | None` (значения `None` / `"step_limit"` / `"tool_errors"`).

- [ ] **Step 1: Написать падающий тест**

```python
# tests/test_terminal_outcome.py
from states import State

def _make_state(max_steps=2):
    # Сигнатура из существующих тестов (tests/test_processing_responses_feedback.py):
    return State(
        verbose=False, model="CodeChat", tools_spec=[], tools_descr=[],
        skills_list=[], max_steps=max_steps, system_prompt_id="v1",
        use_server_subagents=False,
    )

def test_terminal_outcome_defaults_none():
    assert _make_state().terminal_outcome is None
```

- [ ] **Step 2: Прогнать — падает**

Run: `pytest tests/test_terminal_outcome.py -v`
Expected: FAIL (нет атрибута).

- [ ] **Step 3: Добавить поле в State**

`src/states.py`, после `self.finish_gate_rejection_count: int = 0` (88):

```python
        self.terminal_outcome: Optional[str] = None  # None | "step_limit" | "tool_errors"
```

> `Optional` уже импортирован в states.py (используется в типах). Если нет — добавить `from typing import Optional`.

- [ ] **Step 4: Прогнать — зелёный**

Run: `pytest tests/test_terminal_outcome.py -v`
Expected: PASS.

- [ ] **Step 5: Выставлять исход в точках терминирования**

`processing_responses.py:541-545` — заменить:

```python
            if state.steps > state.max_steps:
                state.terminal_outcome = "step_limit"
                return f'Agent executed {state.max_steps} steps and doesn\'t finish his work because of step limits.'

            if tool_error_cnt > max_tool_errors:
                state.terminal_outcome = "tool_errors"
                return f'Agent tools call errors.'
```

- [ ] **Step 6: Поведенческий тест выставления исхода**

Взять за образец существующий harness в `tests/test_run_agent_text_answer.py` (там `run_agent` поднимается с monkeypatch бэкенда). Сконструировать прогон, упирающийся в `max_steps` (бэкенд-стаб, который на каждый вызов возвращает ответ без tool-call/finish → шаги растут до лимита), и утверждать `state.terminal_outcome == "step_limit"` после возврата.

```python
# дополнить tests/test_terminal_outcome.py — паттерн backend-stub см. в
# tests/test_run_agent_text_answer.py (monkeypatch create_call_headers +
# request_backend_*). Здесь — только суть проверки:
def test_step_limit_sets_terminal_outcome(monkeypatch):
    # ... поднять run_agent со стаб-бэкендом, max_steps=1, без finish ...
    # после возврата:
    assert state.terminal_outcome == "step_limit"
```

> Полную обвязку backend-stub скопировать из `test_run_agent_text_answer.py` (не выдумывать сигнатуры `run_agent` — взять рабочие из этого файла).

Run: `pytest tests/test_terminal_outcome.py -v`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add src/states.py src/processing_responses.py tests/test_terminal_outcome.py
git commit -m "feat(harness): State.terminal_outcome — различать лимитные исходы (step_limit/tool_errors)"
```

### Task D2: Мост executor→runner→app

**Files:**
- Modify: `src/agent_executor.py` (`__init__` — `self.last_terminal_outcome`; после `run_agent` на `399`)
- Modify: `src/tui/services/agent_runner.py` (`__init__` — `on_complete`; в `start` после `executor.run` на `171`)
- Modify: `src/tui/app.py` (`__init__:54-55` — флаг; `get_runner:82-84` — wiring)
- Modify: `src/tui/screens/main.py` (сброс флага рядом с `1415-1416`)
- Test: `tests/test_executor_terminal_outcome.py`, `tests/tui/test_runner_on_complete.py`

**Interfaces:**
- Produces: `AgentExecutor.last_terminal_outcome: str | None`; `AgentRunner(..., on_complete: Callable[[str | None], None] | None)`; `app.last_terminal_outcome: str | None`.

- [ ] **Step 1: Написать падающий тест (executor exposes outcome)**

```python
# tests/test_executor_terminal_outcome.py
from types import SimpleNamespace
from agent_executor import AgentExecutor

def test_executor_inits_last_terminal_outcome():
    # AgentExecutor.__init__(self, ctx) только сохраняет ctx и заводит локи —
    # лёгкий SimpleNamespace в роли ctx достаточен.
    ex = AgentExecutor(SimpleNamespace())
    assert ex.last_terminal_outcome is None
```

- [ ] **Step 2: Прогнать — падает**

Run: `pytest tests/test_executor_terminal_outcome.py -v`
Expected: FAIL.

- [ ] **Step 3: Инициализация + установка в executor**

`agent_executor.py`, в `__init__` (рядом с `self._state` инициализацией; см. `64`-область) добавить:

```python
        self.last_terminal_outcome: Optional[str] = None
```

После возврата `run_agent` (`agent_executor.py:399`, сразу после закрывающей скобки вызова `response = run_agent(...)`):

```python
            self.last_terminal_outcome = getattr(state, "terminal_outcome", None)
```

- [ ] **Step 4: on_complete в AgentRunner**

`agent_runner.py:__init__` — добавить параметр и поле:

```python
        on_complete: Optional[Callable[[Optional[str]], None]] = None,
```
```python
        self._on_complete = on_complete
```

В `start()._target`, сразу после `executor.run(...)` (после `171`, внутри `try`):

```python
                        outcome = getattr(executor, "last_terminal_outcome", None)
                        if self._on_complete is not None:
                            try:
                                self._on_complete(outcome)
                            except Exception:
                                pass
```

- [ ] **Step 5: Wiring во флаг app**

`app.py:__init__` (рядом с `54-55`):

```python
        self.last_terminal_outcome: Optional[str] = None
```

`app.py:get_runner` — добавить callback и передать в конструктор:

```python
        def _on_runner_complete(outcome: Optional[str]) -> None:
            self.last_terminal_outcome = outcome

        self.active_runner = AgentRunner(
            executor_factory=factory, on_error=_on_runner_error,
            on_complete=_on_runner_complete,
        )
```

Сброс флага вместе с `last_runner_error` (`main.py:1415-1416`):

```python
        self.app.last_terminal_outcome = None
```

- [ ] **Step 6: Тест on_complete у runner**

```python
# tests/tui/test_runner_on_complete.py
from tui.services.agent_runner import AgentRunner

class _Exec:
    last_terminal_outcome = "step_limit"
    def add_message(self, m): pass
    def run(self, *a, **k): return "done"
    def stop_run(self): pass

def test_on_complete_receives_outcome():
    got = {}
    r = AgentRunner(executor=_Exec(), on_complete=lambda o: got.setdefault("o", o))
    r.start(query="q", thread_id="t")
    r.wait(timeout=5)
    assert got.get("o") == "step_limit"
```

Run: `uv run pytest tests/tui/test_runner_on_complete.py -v` и `pytest tests/test_executor_terminal_outcome.py -v`
Expected: PASS.

- [ ] **Step 7: Коммит**

```bash
git add src/agent_executor.py src/tui/services/agent_runner.py src/tui/app.py src/tui/screens/main.py tests/test_executor_terminal_outcome.py tests/tui/test_runner_on_complete.py
git commit -m "feat(tui): мост terminal_outcome из State в app-флаг (executor→runner→app)"
```

### Task D3: Новые состояния индикатора `stopped_steps` / `stopped_errors`

**Files:**
- Modify: `src/tui/widgets/thinking_indicator.py` (константы `123-125`; `set_state:163-164`; `_render_text:206-226`)
- Test: `tests/tui/test_indicator_stopped_states.py`

**Interfaces:**
- Produces: состояния `"stopped_steps"`, `"stopped_errors"` для `ThinkingIndicator.set_state(state)` / `__init__(state=...)`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/tui/test_indicator_stopped_states.py
from tui.widgets.thinking_indicator import ThinkingIndicator

def _text(state):
    ind = ThinkingIndicator(state=state, clock=lambda: 0.0)
    return ind._render_text().plain

def test_stopped_steps_text():
    assert _text("stopped_steps") == "⏹ Остановлено: превышен лимит шагов"

def test_stopped_errors_text():
    assert _text("stopped_errors") == "⏹ Остановлено: превышен лимит ошибок"
```

- [ ] **Step 2: Прогнать — падает**

Run: `uv run pytest tests/tui/test_indicator_stopped_states.py -v`
Expected: FAIL (state не распознан → ветка thinking).

- [ ] **Step 3: Добавить константы**

`thinking_indicator.py` после `_STATE_FAILED = "failed"` (125):

```python
_STATE_STOPPED_STEPS = "stopped_steps"
_STATE_STOPPED_ERRORS = "stopped_errors"
```

- [ ] **Step 4: Разрешить в set_state**

`set_state` (163-164) — расширить кортеж допустимых:

```python
        if state not in (_STATE_THINKING, _STATE_VALIDATING, _STATE_COMPRESSING,
                         _STATE_PROCESSING, _STATE_DONE, _STATE_CANCELLED, _STATE_FAILED,
                         _STATE_STOPPED_STEPS, _STATE_STOPPED_ERRORS):
            return
```

- [ ] **Step 5: Ветки рендера**

В `_render_text`, после ветки `_STATE_FAILED` (226):

```python
        if self._state == _STATE_STOPPED_STEPS:
            text.append("⏹", style=_DIM)
            text.append(" ")
            text.append("Остановлено: превышен лимит шагов", style=_DIM)
            return text
        if self._state == _STATE_STOPPED_ERRORS:
            text.append("⏹", style=_DIM)
            text.append(" ")
            text.append("Остановлено: превышен лимит ошибок", style=_DIM)
            return text
```

- [ ] **Step 6: Прогнать — зелёный**

Run: `uv run pytest tests/tui/test_indicator_stopped_states.py -v`
Expected: 2 PASS.

- [ ] **Step 7: Коммит**

```bash
git add src/tui/widgets/thinking_indicator.py tests/tui/test_indicator_stopped_states.py
git commit -m "feat(tui): терминальные состояния индикатора «превышен лимит шагов/ошибок»"
```

### Task D4: Ветка спадающего фронта + распознавание kind при restore

**Files:**
- Modify: `src/tui/screens/main.py` (спадающий фронт `1521-1545`: ветки outcome, `_KIND`, `_STATUS`; restore `_restore_terminal_indicator:671`)
- Test: `tests/tui/test_falling_edge_outcome.py`

**Interfaces:**
- Consumes: `app.last_terminal_outcome`, `app.last_runner_error`, `app.last_stop_requested`; `self._store.update_tui_meta(tid, patch)`.

- [ ] **Step 1: Написать падающий тест**

```python
# tests/tui/test_falling_edge_outcome.py
# Проверяем чистую функцию выбора outcome (вынесем её из спадающего фронта).
from tui.screens.main import resolve_terminal_outcome

def test_step_limit_maps_to_stopped_steps():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome="step_limit",
    ) == "stopped_steps"

def test_tool_errors_maps_to_stopped_errors():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome="tool_errors",
    ) == "stopped_errors"

def test_error_and_stop_take_priority():
    assert resolve_terminal_outcome(
        runner_error="boom", stop_requested=False, terminal_outcome="step_limit",
    ) == "failed"
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=True, terminal_outcome="step_limit",
    ) == "stopped"

def test_default_completed():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome=None,
    ) == "completed"
```

- [ ] **Step 2: Прогнать — падает**

Run: `uv run pytest tests/tui/test_falling_edge_outcome.py -v`
Expected: FAIL (`resolve_terminal_outcome` нет).

- [ ] **Step 3: Вынести чистую функцию выбора исхода**

В `src/tui/screens/main.py` (модульный уровень, рядом с импортами):

```python
def resolve_terminal_outcome(*, runner_error, stop_requested, terminal_outcome):
    """Выбор терминального исхода по флагам прогона. Порядок важен:
    error → stop → лимитные исходы → completed (дефолт)."""
    if runner_error:
        return "failed"
    if stop_requested:
        return "stopped"
    if terminal_outcome == "step_limit":
        return "stopped_steps"
    if terminal_outcome == "tool_errors":
        return "stopped_errors"
    return "completed"
```

- [ ] **Step 4: Применить в спадающем фронте**

`main.py:1521-1544` — заменить вычисление `outcome` и запись meta:

```python
            outcome = resolve_terminal_outcome(
                runner_error=getattr(self.app, "last_runner_error", None),
                stop_requested=getattr(self.app, "last_stop_requested", False),
                terminal_outcome=getattr(self.app, "last_terminal_outcome", None),
            )
            # Прямой stop в обход _cmd_stop (active_runner НЕ обнулён) → не «completed».
            if outcome == "completed" and getattr(runner, "_stop_requested", False):
                outcome = "stopped"
            _STATUS = {"completed": "completed", "stopped": "stopped", "failed": "failed",
                       "stopped_steps": "stopped", "stopped_errors": "stopped"}
            _KIND = {"completed": "done", "stopped": "cancelled", "failed": "failed",
                     "stopped_steps": "stopped_steps", "stopped_errors": "stopped_errors"}
            _elapsed = None
            if outcome == "completed" and self._thinking_started_at is not None:
                _elapsed = int(time.monotonic() - self._thinking_started_at)
            self._store.update_tui_meta(
                self._active_task_id,
                {"status": _STATUS[outcome],
                 "last_indicator": {"kind": _KIND[outcome], "elapsed": _elapsed}},
            )
```

> `TaskSummary.status` остаётся валидным `Status` (`stopped`), различие лимитного стопа несёт `last_indicator.kind`. Сознательный компромисс из спеки (§ остаточные проверки): в списке сессий лимит и юзер-отмена сольются в `stopped`.

- [ ] **Step 5: Распознавать новые kind при restore**

`_restore_terminal_indicator:671` — расширить список допустимых kind:

```python
        if isinstance(last, dict) and last.get("kind") in (
            "done", "cancelled", "failed", "stopped_steps", "stopped_errors"
        ):
```

> ПОПРАВКА (по финальному review): `_TERMINAL_STATE` на `main.py:1659` используется не только в legacy-restore, но и в **живом idle-рендере** `_poll_active` — он читал схлопнутый `status` и показывал `■ Отменено` для лимитных стопов на живом прогоне (честный текст появлялся лишь при переоткрытии). Фикс (commit `72d9a1d3`): вынесен чистый хелпер `resolve_live_terminal_indicator_state(*, last_indicator_kind, status, stop_requested)`, idle-ветка предпочитает `last_indicator.kind` (`stopped_steps`/`stopped_errors`), сохраняя guard прямого stop. Legacy-фолбэк без `last_indicator` по-прежнему даёт `cancelled` (принятый компромисс).

- [ ] **Step 6: Прогнать — зелёный + снапшот-регресс**

Run: `uv run pytest tests/tui/test_falling_edge_outcome.py -v`
Expected: 4 PASS.
Run: `uv run pytest tests/tui -q`
Expected: без новых падений.

- [ ] **Step 7: Коммит**

```bash
git add src/tui/screens/main.py tests/tui/test_falling_edge_outcome.py
git commit -m "feat(tui): лимитный исход → честный терминальный статус «превышен лимит шагов/ошибок»"
```

---

## Финальная верификация

- [ ] Полный прогон харнес-тестов: `pytest tests/ -q` — без новых падений vs baseline.
- [ ] Полный прогон TUI: `uv run pytest tests/tui -q` — подозрительные снапшоты перепроверить в изоляции.
- [ ] Ручной smoke в TUI (по возможности): прогнать сценарий с заведомо невалидным MCP-вызовом → убедиться, что строка-вызов сразу `✗`, таймер не бежит, дублирующей `[!]` нет; на `max_steps` индикатор показывает «⏹ Остановлено: превышен лимит шагов».

## Остаточные проверки (из спеки — закрыть в ходе реализации)

- **C1/отправка в бэкенд — ЗАКРЫТО (Task C1b):** `chat_completions` срезает call_id формат-конвертером (`_legacy_role_to_chat_completions_message` → свежий `{role, content}`); `responses` слал бы `input` дословно — добавлен strip в `create_call_data_responses` (Task C1b), не мутирующий persist/TUI-копию. Сериализация для саммари ключ игнорирует (`src/message_format/serialization.py`).
- **A1:** `ToolCallRow.set_result` идемпотентен при повторной финализации (orphan-repair output не шлёт, конкуренции нет — но подтвердить).

## Вне scope (зафиксировано в спеке)

Детектор зацикленных идентичных повторов (fast-follow), slug-нормализация, класс «галлюцинация несуществующего ресурса». Фундамент для детектора (`state.terminal_outcome` + проводка) заложен Группой D.
