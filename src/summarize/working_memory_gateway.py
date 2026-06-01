"""Gateway for working_memory snapshot/render/parse operations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping

REGISTER_GOAL = "goal"
REGISTER_CONSTRAINTS = "constraints"
REGISTER_STATE = "state"
REGISTER_KNOWLEDGE = "knowledge"

REGISTER_ORDER = (
    REGISTER_GOAL,
    REGISTER_CONSTRAINTS,
    REGISTER_STATE,
    REGISTER_KNOWLEDGE,
)

REGISTER_MERGE_MODES = {
    REGISTER_KNOWLEDGE: "append",
    REGISTER_CONSTRAINTS: "append",
    REGISTER_GOAL: "replace",
    REGISTER_STATE: "replace",
}

@dataclass(frozen=True)
class WorkingMemorySnapshot:
    goal: str = ""
    constraints: str = ""
    state: str = ""
    knowledge: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            REGISTER_GOAL: self.goal,
            REGISTER_CONSTRAINTS: self.constraints,
            REGISTER_STATE: self.state,
            REGISTER_KNOWLEDGE: self.knowledge,
        }

@dataclass(frozen=True)
class WorkingMemoryDeltas:
    goal: str = ""
    constraints: str = ""
    state: str = ""
    knowledge: str = ""

    def to_dict(self, *, skip_empty: bool = True) -> Dict[str, str]:
        payload = {
            REGISTER_GOAL: self.goal,
            REGISTER_CONSTRAINTS: self.constraints,
            REGISTER_STATE: self.state,
            REGISTER_KNOWLEDGE: self.knowledge,
        }
        if not skip_empty:
            return payload
        return {name: value for name, value in payload.items() if value}

    def is_empty(self) -> bool:
        return not any((self.goal, self.constraints, self.state, self.knowledge))


def _normalize_register_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _snapshot_from_mapping(raw: Mapping[str, Any] | None) -> WorkingMemorySnapshot:
    data = raw if isinstance(raw, Mapping) else {}
    return WorkingMemorySnapshot(
        goal=_normalize_register_text(data.get(REGISTER_GOAL)),
        constraints=_normalize_register_text(data.get(REGISTER_CONSTRAINTS)),
        state=_normalize_register_text(data.get(REGISTER_STATE)),
        knowledge=_normalize_register_text(data.get(REGISTER_KNOWLEDGE)),
    )


def _deltas_from_mapping(raw: Mapping[str, Any] | None) -> WorkingMemoryDeltas:
    data = raw if isinstance(raw, Mapping) else {}
    return WorkingMemoryDeltas(
        goal=_normalize_register_text(data.get(REGISTER_GOAL)),
        constraints=_normalize_register_text(data.get(REGISTER_CONSTRAINTS)),
        state=_normalize_register_text(data.get(REGISTER_STATE)),
        knowledge=_normalize_register_text(data.get(REGISTER_KNOWLEDGE)),
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


def _register_tags_for_repair_prompt() -> str:
    return ", ".join(f"<{name}>...</{name}>" for name in REGISTER_ORDER)


def render_persistent_state_for_prompt(snapshot: WorkingMemorySnapshot) -> str:
    parts = [
        "<persistent_state>",
        "<working_memory>",
        f"<{REGISTER_GOAL}>",
        snapshot.goal,
        f"</{REGISTER_GOAL}>",
        f"<{REGISTER_CONSTRAINTS}>",
        snapshot.constraints,
        f"</{REGISTER_CONSTRAINTS}>",
        f"<{REGISTER_STATE}>",
        snapshot.state,
        f"</{REGISTER_STATE}>",
        f"<{REGISTER_KNOWLEDGE}>",
        snapshot.knowledge,
        f"</{REGISTER_KNOWLEDGE}>",
        "</working_memory>",
        "</persistent_state>",
    ]
    return "\n".join(parts)


def resolve_persistent_state_block(
    state: Any,
    persistent_state_block: str | None = None,
) -> str:
    """Возвращает готовый блок для prompt: явный аргумент или snapshot из state."""
    if persistent_state_block is not None:
        return str(persistent_state_block)
    return render_persistent_state_for_prompt(read_snapshot_from_state(state))


def build_memory_deltas_format_repair_messages(raw_output: str) -> List[Dict[str, str]]:
    register_tags = _register_tags_for_repair_prompt()
    system_prompt = (
        "You are a strict formatter. Convert input text into working_memory register tags only. "
        f"Use zero or more of: {register_tags}. "
        "Do not add new facts. Preserve meaning. "
        "Append step: omit empty append-register tags. "
        "Replace step: keep empty replace-register tags (empty body means clear)."
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


def parse_memory_deltas(raw_output: str) -> tuple[WorkingMemoryDeltas, frozenset[str]]:
    """Parse register tags; return deltas and which register names were present in output."""
    text = str(raw_output or "")
    deltas: Dict[str, str] = {}
    present: set[str] = set()
    for name in REGISTER_ORDER:
        pattern = rf"<{name}>\s*(.*?)\s*</{name}>"
        matches = list(re.finditer(pattern, text, re.IGNORECASE | re.DOTALL))
        if not matches:
            continue
        value = (matches[-1].group(1) or "").strip()
        if value:
            deltas[name] = value
            present.add(name)
        elif REGISTER_MERGE_MODES.get(name) == "replace":
            deltas[name] = ""
            present.add(name)
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
) -> tuple[WorkingMemoryDeltas, frozenset[str], bool, str]:
    first_pass, first_present = parse_memory_deltas(raw_output)
    if not should_attempt_memory_deltas_repair(
        raw_output,
        first_pass,
        min_chars_for_repair=min_chars_for_repair,
    ):
        return first_pass, first_present, False, str(raw_output or "")
    if repair_fn is None:
        return first_pass, first_present, False, str(raw_output or "")
    repaired_output = str(repair_fn(str(raw_output or "")) or "")
    repaired_pass, repaired_present = parse_memory_deltas(repaired_output)
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
) -> WorkingMemorySnapshot:
    delta_map: Mapping[str, str] = coerce_working_memory_deltas(deltas).to_dict(
        skip_empty=False
    )
    merged: Dict[str, str] = snapshot.to_dict()
    for name in REGISTER_ORDER:
        if REGISTER_MERGE_MODES.get(name) != "replace":
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
    updated = apply_append_deltas(
        snapshot, append_deltas, touched=append_touched
    )
    return apply_replace_deltas(
        updated, replace_deltas, touched=replace_touched
    )


def render_working_memory_for_summary_message(snapshot: WorkingMemorySnapshot) -> str:
    parts = ["<working_memory>"]
    for name in REGISTER_ORDER:
        value = _normalize_register_text(snapshot.to_dict().get(name))
        if not value:
            continue
        parts.append(f"<{name}>")
        parts.append(value)
        parts.append(f"</{name}>")
    parts.append("</working_memory>")
    return "\n".join(parts)


__all__ = [
    "WorkingMemorySnapshot",
    "WorkingMemoryDeltas",
    "REGISTER_ORDER",
    "REGISTER_MERGE_MODES",
    "MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL",
    "read_snapshot_from_state",
    "render_persistent_state_for_prompt",
    "resolve_persistent_state_block",
    "build_memory_deltas_format_repair_messages",
    "parse_memory_deltas",
    "should_attempt_memory_deltas_repair",
    "parse_memory_deltas_with_repair",
    "coerce_working_memory_deltas",
    "apply_append_deltas",
    "apply_replace_deltas",
    "apply_memory_patch",
    "render_working_memory_for_summary_message",
]
