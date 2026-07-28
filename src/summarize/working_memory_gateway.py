"""Gateway for session-memory snapshot/render/parse (internal name: working_memory).

Naming map (same four registers everywhere):
- ``state.shared.working_memory`` / JSON ``shared.working_memory`` — persist
- ``session_memory`` — builtin tool for the agent
- ``<session_memory>`` inside ``<persistent_state>`` — summary / patch prompts

1) **Trace** — ``session_memory`` tool: ``append`` or ``write`` (full overwrite) on any register.
2) **Summarization** — LLM patch via ``apply_summary_memory_patch``: full replace.

Parsed/applied gateway deltas always use replace semantics (``REGISTER_MERGE_MODES``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping

REGISTER_GOAL = "goal"
REGISTER_CONSTRAINTS = "constraints"
REGISTER_STATE = "state"
REGISTER_KNOWLEDGE = "knowledge"

SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER = "{{SESSION_MEMORY_REGISTER_NAMES}}"
MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER = "{{MEMORY_PATCH_WM_REGISTERS}}"


@dataclass(frozen=True)
class SessionMemoryRegister:
    """Канон одного WM-регистра: имя и replace-правило для patch."""

    name: str
    replace_rule: str


SESSION_MEMORY_REGISTERS: tuple[SessionMemoryRegister, ...] = (
    SessionMemoryRegister(
        name=REGISTER_GOAL,
        replace_rule=(
            "current objective and definition of done (no artifact catalogs, no sha256 lists)"
        ),
    ),
    SessionMemoryRegister(
        name=REGISTER_CONSTRAINTS,
        replace_rule=(
            "hard requirements and preferences still binding (no TODO lists, no repeated goal prose)"
        ),
    ),
    SessionMemoryRegister(
        name=REGISTER_STATE,
        replace_rule=(
            "only unresolved work for the current or next turn — [TODO]/[IN_PROGRESS]/[BLOCKED]; "
            "no [DONE] or verification transcripts; if finished with no blocker use "
            "`- Idle until user sends a new request.` or `- n/a`"
        ),
    ),
    SessionMemoryRegister(
        name=REGISTER_KNOWLEDGE,
        replace_rule=(
            "stable verified facts and evidence; full absolute paths as in tool results "
            "(e.g. `/home/.../src/backends/local.py:84`, never basename-only), checksums, command verdicts "
            "(one line each; dedupe persistent_state; cap superseded noise)"
        ),
    ),
)

REGISTER_ORDER: tuple[str, ...] = tuple(r.name for r in SESSION_MEMORY_REGISTERS)

# Parsed/applied LLM memory patches: every register is a full replace (not append).
# The ``session_memory`` tool trace path does not use this map.
REGISTER_MERGE_MODES: Dict[str, str] = {name: "replace" for name in REGISTER_ORDER}

SUMMARY_PATCH_REPLACE_REGISTERS: frozenset[str] = frozenset(REGISTER_ORDER)

REGISTER_PATCH_RULES: Dict[str, str] = {
    r.name: r.replace_rule for r in SESSION_MEMORY_REGISTERS
}


def render_session_memory_register_names_for_prompt() -> str:
    return ", ".join(REGISTER_ORDER)


def render_memory_patch_registers_for_prompt() -> str:
    n = len(REGISTER_ORDER)
    shape = "\n".join(f"<{name}>...</{name}>" for name in REGISTER_ORDER)
    rules = "\n".join(
        f"- **{reg.name}:** {reg.replace_rule}" for reg in SESSION_MEMORY_REGISTERS
    )
    return (
        f"Return **full snapshots** for **all {n}** registers on every run "
        "(replace semantics downstream):\n\n"
        f"{shape}\n\n"
        "Rules:\n\n"
        "- Text inside each tag is the **entire** new register value.\n"
        f"- **Always include all {n} tags** when possible. Omitting a tag leaves the old value.\n"
        "- Reconcile dialog + summary vs `<persistent_state>`: drop outdated items; "
        "prefer newer dialog/summary on conflict.\n"
        "- **One home per fact** across registers.\n"
        f"{rules}\n"
        "- Empty tag body clears that register — rare during active work.\n"
        "- No verbose tool logs. No facts unsupported by the inputs."
    )


def render_memory_deltas_format_repair_system_prompt(*, full_replace: bool = False) -> str:
    register_tags = ", ".join(f"<{name}>...</{name}>" for name in REGISTER_ORDER)
    n = len(REGISTER_ORDER)
    replace_hint = (
        f"Use all {n} register tags when the source covers a full memory rebuild; "
        "keep empty tags when the source meant to clear a register (empty body means clear)."
    )
    del full_replace  # all gateway registers use replace semantics
    return (
        "You are a strict formatter. Convert input text into session memory register tags only. "
        f"Use zero or more of: {register_tags}. "
        "Do not add new facts. Preserve meaning. "
        f"{replace_hint}"
    )


def inject_working_memory_prompt_placeholders(prompt: str) -> str:
    """Подставляет WM-плейсхолдеры из ``SESSION_MEMORY_REGISTERS``."""
    if SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            SESSION_MEMORY_REGISTER_NAMES_PLACEHOLDER,
            render_session_memory_register_names_for_prompt(),
        )
    if MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER in prompt:
        prompt = prompt.replace(
            MEMORY_PATCH_WM_REGISTERS_PLACEHOLDER,
            render_memory_patch_registers_for_prompt(),
        )
    return prompt


class _RegisterBagMixin:
    """Атрибуты по одному на каждый ``SESSION_MEMORY_REGISTERS[].name``."""

    def __init__(self, **kwargs: Any) -> None:
        for name in REGISTER_ORDER:
            object.__setattr__(
                self, name, _normalize_register_text(kwargs.get(name))
            )

    def to_dict(self) -> Dict[str, str]:
        return {name: getattr(self, name) for name in REGISTER_ORDER}

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return False
        return all(getattr(self, n) == getattr(other, n) for n in REGISTER_ORDER)

    def __repr__(self) -> str:
        parts = [
            f"{name}={getattr(self, name)!r}"
            for name in REGISTER_ORDER
            if getattr(self, name)
        ]
        return f"{type(self).__name__}({', '.join(parts)})"


class WorkingMemorySnapshot(_RegisterBagMixin):
    """Снимок регистров WM; состав полей — из ``SESSION_MEMORY_REGISTERS``."""


class WorkingMemoryDeltas(_RegisterBagMixin):
    """Дельты patch; парсятся regex по тем же именам регистров."""

    def to_dict(self, *, skip_empty: bool = True) -> Dict[str, str]:
        payload = {name: getattr(self, name) for name in REGISTER_ORDER}
        if not skip_empty:
            return payload
        return {name: value for name, value in payload.items() if value}

    def is_empty(self) -> bool:
        return not any(getattr(self, name) for name in REGISTER_ORDER)


def _normalize_register_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _snapshot_from_mapping(raw: Mapping[str, Any] | None) -> WorkingMemorySnapshot:
    data = raw if isinstance(raw, Mapping) else {}
    return WorkingMemorySnapshot(
        **{name: _normalize_register_text(data.get(name)) for name in REGISTER_ORDER}
    )


def _deltas_from_mapping(raw: Mapping[str, Any] | None) -> WorkingMemoryDeltas:
    data = raw if isinstance(raw, Mapping) else {}
    return WorkingMemoryDeltas(
        **{name: _normalize_register_text(data.get(name)) for name in REGISTER_ORDER}
    )


def coerce_working_memory_deltas(
    raw: WorkingMemoryDeltas | Mapping[str, Any] | None,
) -> WorkingMemoryDeltas:
    """Normalize parsed or serialized deltas to a gateway DTO."""
    if isinstance(raw, WorkingMemoryDeltas):
        return raw
    return _deltas_from_mapping(raw)


def read_snapshot_from_state(state: Any) -> WorkingMemorySnapshot:
    shared = getattr(state, "shared", None)
    if shared is None:
        return WorkingMemorySnapshot()
    return _snapshot_from_mapping(getattr(shared, "working_memory", None))


MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL = "working_memory_deltas_format_repair"


def render_persistent_state_for_prompt(snapshot: WorkingMemorySnapshot) -> str:
    data = snapshot.to_dict()
    parts = ["<persistent_state>", "<session_memory>"]
    for name in REGISTER_ORDER:
        parts.extend((f"<{name}>", data.get(name, ""), f"</{name}>"))
    parts.extend(("</session_memory>", "</persistent_state>"))
    return "\n".join(parts)


def resolve_persistent_state_block(
    state: Any,
    persistent_state_block: str | None = None,
) -> str:
    """Возвращает готовый блок для prompt: явный аргумент или snapshot из state."""
    if persistent_state_block is not None:
        return str(persistent_state_block)
    return render_persistent_state_for_prompt(read_snapshot_from_state(state))


def build_memory_deltas_format_repair_messages(
    raw_output: str,
    *,
    full_replace: bool = False,
) -> List[Dict[str, str]]:
    system_prompt = render_memory_deltas_format_repair_system_prompt(
        full_replace=full_replace
    )
    user_prompt = (
        "<raw_memory_step_output>\n"
        f"{str(raw_output or '')}\n"
        "</raw_memory_step_output>\n"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _register_uses_replace_semantics(name: str, *, full_replace: bool = False) -> bool:
    del full_replace  # all gateway registers use replace semantics
    return name in REGISTER_ORDER and REGISTER_MERGE_MODES.get(name) == "replace"


def _build_register_body_patterns() -> dict[str, re.Pattern[str]]:
    """Regex по одному на каждый ``SESSION_MEMORY_REGISTERS[].name`` — whitelist для extract."""
    return {
        reg.name: re.compile(
            rf"<{re.escape(reg.name)}>\s*(.*?)\s*</{re.escape(reg.name)}>",
            re.DOTALL,
        )
        for reg in SESSION_MEMORY_REGISTERS
    }


_REGISTER_BODY_PATTERNS = _build_register_body_patterns()


def extract_register_bodies(text: str) -> dict[str, str]:
    """Вырезает тела регистров ``<name>…</name>`` (последний match); пустое тело → ``''``."""
    s = (text or "").strip()
    if not s:
        return {}
    bodies: dict[str, str] = {}
    for reg in SESSION_MEMORY_REGISTERS:
        pattern = _REGISTER_BODY_PATTERNS[reg.name]
        last_match: re.Match[str] | None = None
        for match in pattern.finditer(s):
            last_match = match
        if last_match is not None:
            bodies[reg.name] = last_match.group(1).strip()
    return bodies


def parse_memory_deltas(
    raw_output: str,
    *,
    full_replace: bool = False,
) -> tuple[WorkingMemoryDeltas, frozenset[str]]:
    """Parse register tags; return deltas and which register names were present in output.

    Expects already-projected aux LLM text (choke in ``request_summary_text``).
    """
    del full_replace  # kept for call-site compatibility
    bodies = extract_register_bodies(str(raw_output or ""))
    deltas: Dict[str, str] = {}
    present: set[str] = set()
    for reg in SESSION_MEMORY_REGISTERS:
        if reg.name not in bodies:
            continue
        value = bodies[reg.name]
        if value:
            deltas[reg.name] = value
            present.add(reg.name)
        elif _register_uses_replace_semantics(reg.name):
            deltas[reg.name] = ""
            present.add(reg.name)
    return _deltas_from_mapping(deltas), frozenset(present)


def should_attempt_memory_deltas_repair(
    raw_output: str,
    deltas: WorkingMemoryDeltas,
    *,
    min_chars_for_repair: int = 100,
) -> bool:
    if not isinstance(deltas, WorkingMemoryDeltas):
        return False
    if not deltas.is_empty():
        return False
    return len(str(raw_output or "").strip()) >= max(1, int(min_chars_for_repair))


def parse_memory_deltas_with_repair(
    raw_output: str,
    *,
    repair_fn: Callable[[str], str] | None = None,
    min_chars_for_repair: int = 100,
    full_replace: bool = False,
) -> tuple[WorkingMemoryDeltas, frozenset[str], bool, str]:
    first_pass, first_present = parse_memory_deltas(
        raw_output, full_replace=full_replace
    )
    if not should_attempt_memory_deltas_repair(
        raw_output,
        first_pass,
        min_chars_for_repair=min_chars_for_repair,
    ):
        return first_pass, first_present, False, str(raw_output or "")
    if repair_fn is None:
        return first_pass, first_present, False, str(raw_output or "")
    repaired_output = str(repair_fn(str(raw_output or "")) or "")
    repaired_pass, repaired_present = parse_memory_deltas(
        repaired_output, full_replace=full_replace
    )
    return repaired_pass, repaired_present, True, repaired_output


def apply_append_deltas(
    snapshot: WorkingMemorySnapshot,
    deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    *,
    touched: frozenset[str] | None = None,
) -> WorkingMemorySnapshot:
    delta_map: Mapping[str, str] = coerce_working_memory_deltas(deltas).to_dict(
        skip_empty=False
    )
    merged: Dict[str, str] = snapshot.to_dict()
    for name in REGISTER_ORDER:
        if REGISTER_MERGE_MODES.get(name) != "append":
            continue
        if touched is not None and name not in touched:
            continue
        delta_text = _normalize_register_text(delta_map.get(name))
        if not delta_text:
            continue
        base_text = _normalize_register_text(merged.get(name))
        merged[name] = f"{base_text}\n\n{delta_text}" if base_text else delta_text
    return _snapshot_from_mapping(merged)


def apply_replace_deltas(
    snapshot: WorkingMemorySnapshot,
    deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    *,
    touched: frozenset[str] | None = None,
    registers: frozenset[str] | None = None,
) -> WorkingMemorySnapshot:
    delta_map: Mapping[str, str] = coerce_working_memory_deltas(deltas).to_dict(
        skip_empty=False
    )
    merged: Dict[str, str] = snapshot.to_dict()
    replace_names = registers if registers is not None else frozenset(
        name for name in REGISTER_ORDER if REGISTER_MERGE_MODES.get(name) == "replace"
    )
    for name in REGISTER_ORDER:
        if name not in replace_names:
            continue
        if touched is not None:
            if name not in touched:
                continue
        elif isinstance(deltas, Mapping) and name not in deltas:
            continue
        elif touched is None and not isinstance(deltas, Mapping):
            if not _normalize_register_text(delta_map.get(name)):
                continue
        merged[name] = _normalize_register_text(delta_map.get(name, ""))
    return _snapshot_from_mapping(merged)


def apply_memory_patch(
    snapshot: WorkingMemorySnapshot,
    append_deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    replace_deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    *,
    append_touched: frozenset[str] | None = None,
    replace_touched: frozenset[str] | None = None,
) -> WorkingMemorySnapshot:
    """Apply LLM patch deltas; all registers use replace (``replace_deltas`` wins on conflict)."""
    append_map = coerce_working_memory_deltas(append_deltas).to_dict()
    replace_map = coerce_working_memory_deltas(replace_deltas).to_dict()
    combined = {**append_map, **replace_map}
    touched: set[str] = set()
    if append_touched:
        touched.update(append_touched)
        append_all = coerce_working_memory_deltas(append_deltas).to_dict(skip_empty=False)
        for name in append_touched:
            combined[name] = append_all.get(name, "")
    if replace_touched:
        touched.update(replace_touched)
        replace_all = coerce_working_memory_deltas(replace_deltas).to_dict(skip_empty=False)
        for name in replace_touched:
            combined[name] = replace_all.get(name, "")
    return apply_replace_deltas(
        snapshot,
        combined,
        touched=frozenset(touched) if touched else None,
    )


def apply_summary_memory_patch(
    snapshot: WorkingMemorySnapshot,
    replace_deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    *,
    replace_touched: frozenset[str] | None = None,
) -> WorkingMemorySnapshot:
    """After summarization: full replace for registers present in the LLM output only.

    Omitted tags are left unchanged (previous snapshot kept). Empty tag body still
    clears that register when the tag was present (``replace_touched``).
    """
    return apply_replace_deltas(
        snapshot,
        replace_deltas,
        touched=replace_touched,
    )


def render_session_memory_for_summary_message(snapshot: WorkingMemorySnapshot) -> str:
    parts = ["<session_memory>"]
    for name in REGISTER_ORDER:
        value = _normalize_register_text(snapshot.to_dict().get(name))
        if not value:
            continue
        parts.append(f"<{name}>")
        parts.append(value)
        parts.append(f"</{name}>")
    parts.append("</session_memory>")
    return "\n".join(parts)


__all__ = [
    "WorkingMemorySnapshot",
    "WorkingMemoryDeltas",
    "REGISTER_ORDER",
    "REGISTER_MERGE_MODES",
    "SUMMARY_PATCH_REPLACE_REGISTERS",
    "MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL",
    "read_snapshot_from_state",
    "render_persistent_state_for_prompt",
    "resolve_persistent_state_block",
    "build_memory_deltas_format_repair_messages",
    "extract_register_bodies",
    "parse_memory_deltas",
    "should_attempt_memory_deltas_repair",
    "parse_memory_deltas_with_repair",
    "coerce_working_memory_deltas",
    "apply_append_deltas",
    "apply_replace_deltas",
    "apply_memory_patch",
    "apply_summary_memory_patch",
    "render_session_memory_for_summary_message",
]
