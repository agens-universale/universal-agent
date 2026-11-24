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

REGISTER_ORDER: tuple[str, ...] = tuple(
    r.name for r in SESSION_MEMORY_REGISTERS
)

# Parsed/applied LLM memory patches: every register is a full replace (not append).
# The ``session_memory`` tool trace path does not use this map.
REGISTER_MERGE_MODES: Dict[str, str] = dict.fromkeys(REGISTER_ORDER, "replace")

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
        f"- **{reg.name}:** {reg.replace_rule}"
        for reg in SESSION_MEMORY_REGISTERS
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


def render_memory_deltas_format_repair_system_prompt(
    *, full_replace: bool = False
) -> str:
    register_tags = ", ".join(
        f"<{name}>...</{name}>" for name in REGISTER_ORDER
    )
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


class _RegisterBagMixin:  # noqa: PLW1641  # сравнение по составу; в множества не кладётся
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
        return all(
            getattr(self, n) == getattr(other, n) for n in REGISTER_ORDER
        )

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


def _snapshot_from_mapping(
    raw: Mapping[str, Any] | None,
) -> WorkingMemorySnapshot:
    data = raw if isinstance(raw, Mapping) else {}
    return WorkingMemorySnapshot(
        **{
            name: _normalize_register_text(data.get(name))
            for name in REGISTER_ORDER
        }
    )


def _deltas_from_mapping(raw: Mapping[str, Any] | None) -> WorkingMemoryDeltas:
    data = raw if isinstance(raw, Mapping) else {}
    return WorkingMemoryDeltas(
        **{
            name: _normalize_register_text(data.get(name))
            for name in REGISTER_ORDER
        }
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


MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL = (
    "working_memory_deltas_format_repair"
)


def render_persistent_state_for_prompt(
    snapshot: WorkingMemorySnapshot,
    session_todo: Any = None,
) -> str:
    # nested: message_policy → tool_usage_notice → states → tools → summarize
    from gc_core.message_policy.todo_context import (
        render_todo_for_persistent_state,
    )

    data = snapshot.to_dict()
    parts = ["<persistent_state>", "<session_memory>"]
    for name in REGISTER_ORDER:
        parts.extend((f"<{name}>", data.get(name, ""), f"</{name}>"))
    parts.append("</session_memory>")
    # Чеклист задачи — рядом с памятью, чтобы сводка пронесла план через сжатие.
    # Пустой чеклист тега не добавляет.
    todo_block = render_todo_for_persistent_state(session_todo)
    if todo_block:
        parts.append(todo_block)
    parts.append("</persistent_state>")
    return "\n".join(parts)


def read_session_todo_from_state(state: Any) -> list:
    raw = getattr(state, "session_todo", None)
    return raw if isinstance(raw, list) else []


def resolve_persistent_state_block(
    state: Any,
    persistent_state_block: str | None = None,
) -> str:
    """Возвращает готовый блок для prompt: явный аргумент или snapshot из state."""
    if persistent_state_block is not None:
        return str(persistent_state_block)
    return render_persistent_state_for_prompt(
        read_snapshot_from_state(state),
        session_todo=read_session_todo_from_state(state),
    )


def build_memory_deltas_format_repair_messages(
    raw_output: str,
    *,
    full_replace: bool = False,
) -> List[Dict[str, str]]:
    system_prompt = render_memory_deltas_format_repair_system_prompt(
        full_replace=full_replace
    )
    user_prompt = f"<raw_memory_step_output>\n{raw_output or ''!s}\n</raw_memory_step_output>\n"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _register_uses_replace_semantics(
    name: str, *, full_replace: bool = False
) -> bool:
    del full_replace  # all gateway registers use replace semantics
    return (
        name in REGISTER_ORDER and REGISTER_MERGE_MODES.get(name) == "replace"
    )


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
_REGISTER_TAG_TOKEN = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9_]*)>")
_REGISTER_NAMES = frozenset(REGISTER_ORDER)
_RESERVED_REGISTER_TAG = re.compile(
    rf"<(/?)({'|'.join(re.escape(name) for name in REGISTER_ORDER)})>"
)
_MARKDOWN_CODE_SPAN = re.compile(r"(?<!\\)(?:\\\\)*(`+)(.*?)\1", re.DOTALL)
_INLINE_CODE_CAPTION = re.compile(r"\A(`+)[^`\n]+\1\s*:?[ \t]*\Z")
_BOLD_REGISTER_CAPTION = re.compile(r"\*\*([A-Za-z]+)\*\*:?")
_REGISTER_BOUNDARY_IN_SPAN = re.compile(
    rf"</(?:{'|'.join(REGISTER_ORDER)})>.*?<(?:{'|'.join(REGISTER_ORDER)})>",
    re.DOTALL,
)
_PATCH_FENCE = re.compile(
    r"^(?P<marker>`{3,})[^`\n]*\n(?P<body>.*?)\n(?P=marker)[ \t]*(?=\n|\Z)",
    re.DOTALL | re.MULTILINE,
)
_DRAFT_FENCE = re.compile(
    r"^[ \t]{0,3}(?P<fence>(?P<marker>[`~])(?P=marker){2,})"
    r"(?!(?P=marker))[^\n]*(?:\n|\Z)"
    r".*?(?:^[ \t]{0,3}(?P=fence)(?P=marker)*[ \t]*\r?$|\Z)",
    re.DOTALL | re.MULTILINE,
)
_EXAMPLE_CUE = re.compile(
    r"\b(?:example|for instance|do not use|пример|для иллюстрации формата)\b|\be\.g\.",
    re.I,
)
_FENCED_EXAMPLE_CAPTION = re.compile(
    r"^[ \t]*(?:for example\b|example\b|e\.g\.|пример\b|"
    r"например\b|для иллюстрации формата\b|do not use[ \t]*:)",
    re.I | re.MULTILINE,
)
_OUTSIDE_TAG_LIKE = re.compile(r"</?[A-Za-z]")
_REGISTER_END_TAG = re.compile(
    rf"</({'|'.join(re.escape(name) for name in REGISTER_ORDER)})>\Z"
)
_BROKEN_REGISTER_TAIL = re.compile(
    rf"</?(?:{'|'.join(re.escape(name) for name in REGISTER_ORDER)})\s*\Z"
)
_REGISTER_WRAPPER_END = re.compile(
    r"(?:</session_memory>\s*)?(?:</persistent_state>\s*)?\Z"
)
_PERSISTENT_STATE_WITH_TODO = re.compile(
    r"<persistent_state>\s*<session_memory>(?P<memory>.*?)</session_memory>\s*"
    r"<session_todo>(?:(?!</session_todo>).)*</session_todo>\s*</persistent_state>",
    re.DOTALL,
)
_THINK_PATCH_BOUNDARY = re.compile(
    rf"</think>\s*(?=<(?:{'|'.join(REGISTER_ORDER)}|persistent_state)>|`{{3,}})"
)
_QUOTED_REGISTER_BLOCK = re.compile(
    rf"""(?P<quote>["'])(?=\s*(?:</think>\s*)?<(?:{"|".join(REGISTER_ORDER)})>)"""
    rf"""(?:(?!(?P=quote)).)*?</(?:{"|".join(REGISTER_ORDER)})>\s*(?P=quote)""",
    re.DOTALL,
)
_SHELL_HEREDOC_HEAD = (
    r"""^[ \t]*(?P<command>[\w./-]+)"""
    r"""(?:[ \t]+(?:[\w./%-]+|"[^"\r\n]*"|'[^'\r\n]*'))*[ \t]*"""
    r"""<<(?P<strip_tabs>-)?[ \t]*(?P<quote>["']?)"""
    r"(?P<label>[A-Za-z_]\w*)(?P=quote)[ \t]*\r?\n"
)
_SHELL_HEREDOC = re.compile(
    _SHELL_HEREDOC_HEAD
    + r".*?^(?P<terminator>(?(strip_tabs)\t*)(?P=label))(?=\r?$)",
    re.DOTALL | re.MULTILINE,
)
_LEADING_HEREDOC = re.compile(_SHELL_HEREDOC_HEAD)
_HEREDOC_COMMANDS = frozenset(
    {"bash", "cat", "dash", "echo", "ksh", "printf", "sh", "tee", "zsh"}
)
_OPTIONAL_SUMMARY_TRAILER = re.compile(r"(?:</summary>(?:\s+[^<>\r\n]+)?)?")
_DRAFT_LITERAL_MARKER = re.compile(r"""[<>`"'“”‘’\\]""")
_DRAFT_PREFIX_LITERAL = re.compile(r"<<|<[!?]|[`\\]|(?m:^[ \t]*(?:>|~{3,}))")
_DRAFT_QUOTATION = re.compile(r"""["“”«»]|(?<!\w)['‘’]|['‘’](?!\w)""")
_DRAFT_TAG_TOKEN = re.compile(r"<(?:[!?]|/?[A-Za-z_][\w.:-]*(?=[\s/>]))")


def _is_literal(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _next_unquoted_tag(
    pattern: re.Pattern[str],
    text: str,
    start: int,
    spans: list[tuple[int, int]],
) -> re.Match[str] | None:
    return next(
        (
            tag
            for tag in pattern.finditer(text, start)
            if not _is_literal(tag.start(), spans)
        ),
        None,
    )


def _next_structural_tag(
    text: str, start: int, spans: list[tuple[int, int]]
) -> re.Match[str] | None:
    for tag in _REGISTER_TAG_TOKEN.finditer(text, start):
        if _is_literal(tag.start(), spans):
            continue
        line_start = text.rfind("\n", start, tag.start()) + 1
        before = text[max(start, line_start) : tag.start()].strip()
        if before:
            if _EXAMPLE_CUE.search(before):
                continue
            bold_caption = _BOLD_REGISTER_CAPTION.fullmatch(before)
            if _INLINE_CODE_CAPTION.fullmatch(before) is None and (
                not (
                    before.endswith(":")
                    or before == ","
                    or (
                        bold_caption is not None
                        and bold_caption.group(1).lower() == tag.group(2)
                    )
                )
                or tag.group(1)
                or tag.group(2) not in _REGISTER_NAMES
                or _matching_register_close(
                    text, tag.end(), tag.group(2), spans
                )
                is None
            ):
                continue
        return tag
    return None


def _register_opening(text: str, cursor: int) -> re.Match[str] | None:
    opening = _REGISTER_TAG_TOKEN.match(text, cursor)
    if (
        opening is None
        or opening.group(1)
        or opening.group(2) not in _REGISTER_NAMES
    ):
        return None
    return opening


def _matching_register_close(
    text: str, start: int, name: str, spans: list[tuple[int, int]]
) -> re.Match[str] | None:
    closing = _next_unquoted_tag(_RESERVED_REGISTER_TAG, text, start, spans)
    if closing is None or closing.group(1) != "/" or closing.group(2) != name:
        return None
    return closing


def _parse_register_block(
    text: str, start: int, spans: list[tuple[int, int]]
) -> dict[str, str] | None:
    """Parse one contiguous, balanced run of register tags from ``start``."""
    bodies: dict[str, str] = {}
    cursor = start
    while cursor < len(text):
        opening = _register_opening(text, cursor)
        if opening is None:
            return None
        name = opening.group(2)
        closing = _matching_register_close(text, opening.end(), name, spans)
        if closing is None:
            return None
        bodies[name] = text[opening.end() : closing.start()].strip()
        cursor = closing.end()
        if _REGISTER_WRAPPER_END.fullmatch(text[cursor:].strip()):
            return bodies
        next_tag = _next_structural_tag(text, cursor, spans)
        if next_tag is None:
            if _BROKEN_REGISTER_TAIL.search(text[cursor:]):
                return None
            return bodies
        if next_tag.group(1) or next_tag.group(2) not in _REGISTER_NAMES:
            return None
        cursor = next_tag.start()
    return None


def _heredoc_overlaps_register_end(
    text: str, heredoc: re.Match[str], literal_spans: list[tuple[int, int]]
) -> bool:
    last_tag = None
    for tag in _RESERVED_REGISTER_TAG.finditer(text, 0, heredoc.start()):
        if not _is_literal(tag.start(), literal_spans):
            last_tag = tag
    if last_tag is None or last_tag.group(1):
        return False
    name = last_tag.group(2)
    closing = _matching_register_close(
        text, heredoc.start(), name, literal_spans
    )
    # A matching close after the heredoc can enclose the entire literal.
    return (
        closing is not None
        and closing.start() < heredoc.end()
        and _matching_register_close(text, heredoc.end(), name, literal_spans)
        is None
    )


def _has_shell_heredoc_context(heredoc: re.Match[str]) -> bool:
    command = heredoc.group("command").rsplit("/", 1)[-1]
    return (
        heredoc.group("quote") != ""
        or heredoc.group("strip_tabs") is not None
        or command in _HEREDOC_COMMANDS
    )


def _heredoc_ends_inside_register(
    text: str, heredoc: re.Match[str], literal_spans: list[tuple[int, int]]
) -> bool:
    for tag in _RESERVED_REGISTER_TAG.finditer(
        text, heredoc.end("label"), heredoc.start("terminator")
    ):
        if tag.group(1) or _is_literal(tag.start(), literal_spans):
            continue
        closing = _matching_register_close(
            text, tag.end(), tag.group(2), literal_spans
        )
        if closing is not None and closing.start() >= heredoc.end():
            return True
    return False


def _command_and_quote_spans(
    text: str, code_spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    spans = [
        match.span()
        for match in _QUOTED_REGISTER_BLOCK.finditer(text)
        if not _is_literal(match.start(), code_spans)
    ]
    for match in _SHELL_HEREDOC.finditer(text):
        literals = code_spans + spans
        if _is_literal(
            match.start(), literals
        ) or _heredoc_overlaps_register_end(text, match, literals):
            continue
        if _has_shell_heredoc_context(
            match
        ) or not _heredoc_ends_inside_register(text, match, literals):
            spans.append(match.span())

    # A missing terminator cannot resolve an ambiguous shift expression.
    leading = _LEADING_HEREDOC.match(text)
    if (
        leading is not None
        and _has_shell_heredoc_context(leading)
        and not _is_literal(0, spans)
    ):
        spans.append((0, len(text)))
    return spans


def _register_literal_spans(
    text: str, *, include_draft_fences: bool = False
) -> list[tuple[int, int]]:
    code_matches = list(_MARKDOWN_CODE_SPAN.finditer(text))
    code_spans = [match.span() for match in code_matches]
    draft_fences = []
    if include_draft_fences:
        draft_fences = [
            match.span()
            for match in _DRAFT_FENCE.finditer(text)
            if not any(
                start < match.start() < end for start, end in code_spans
            )
        ]
    return (
        _command_and_quote_spans(text, code_spans + draft_fences)
        + draft_fences
        + [
            match.span()
            for match in code_matches
            if len(match.group(1)) >= 3
            or _REGISTER_BOUNDARY_IN_SPAN.search(match.group()) is None
            or _REGISTER_BOUNDARY_IN_SPAN.fullmatch(match.group(2)) is not None
        ]
    )


def _extract_balanced_registers(s: str) -> dict[str, str]:
    spans = _register_literal_spans(s)
    for token in _REGISTER_TAG_TOKEN.finditer(s):
        if _is_literal(token.start(), spans):
            continue
        if token.group(1) or token.group(2) not in _REGISTER_NAMES:
            continue
        return _parse_register_block(s, token.start(), spans) or {}
    return {}


def _unwrap_session_memory_with_todo(text: str) -> str:
    wrapper = _PERSISTENT_STATE_WITH_TODO.fullmatch(text)
    return wrapper.group("memory").strip() if wrapper else text


def _select_patch_fence(text: str) -> tuple[str, str] | None:
    """Return the body and surrounding prose of one plausible patch fence."""
    fences = list(_PATCH_FENCE.finditer(text))
    if not fences:
        return None
    outside = _PATCH_FENCE.sub("\n", text)
    fenced = fences[0]
    if len(fences) > 1:
        register_fences = [
            fence
            for fence in fences
            if _RESERVED_REGISTER_TAG.search(fence.group("body"))
        ]
        if len(register_fences) != 1 or "```" in outside:
            return None
        fenced = register_fences[0]
    elif text.count(fenced.group("marker")) != 2:
        return None
    return fenced.group("body").strip(), outside


def _extract_register_candidate(s: str) -> dict[str, str]:
    s = _unwrap_session_memory_with_todo(s)
    selected = _select_patch_fence(s)
    if selected is None:
        return _extract_balanced_registers(s)
    inner, outside = selected
    if _OUTSIDE_TAG_LIKE.search(outside):
        enclosing = _extract_balanced_registers(s)
        return (
            enclosing
            if any("```" in body for body in enclosing.values())
            else {}
        )
    if _FENCED_EXAMPLE_CAPTION.search(outside):
        return {}
    inner = _unwrap_session_memory_with_todo(inner)
    if (
        _RESERVED_REGISTER_TAG.match(inner) is None
        or _REGISTER_END_TAG.search(inner) is None
    ):
        return {}
    return _extract_balanced_registers(inner)


def extract_register_bodies(text: str) -> dict[str, str]:
    """Extract a balanced register block, excluding a formatter preamble."""
    s = _unwrap_session_memory_with_todo((text or "").strip())
    if not s:
        return {}
    code_spans = [match.span() for match in _MARKDOWN_CODE_SPAN.finditer(s)]
    literal_blocks = _command_and_quote_spans(s, code_spans)
    spans = literal_blocks + code_spans
    for boundary in _THINK_PATCH_BOUNDARY.finditer(s):
        if _is_literal(boundary.start(), spans):
            continue
        after_think = _extract_register_candidate(s[boundary.end() :].strip())
        if after_think:
            return after_think
    full = _extract_register_candidate(s)
    if full:
        return full
    for boundary in re.finditer(r"</think>", s):
        if _is_literal(boundary.start(), literal_blocks):
            continue
        after_think = _extract_register_candidate(s[boundary.end() :].strip())
        if after_think:
            return after_think
    return full


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


def _final_register_draft(text: str) -> str:
    if _register_opening(text, 0) is not None:
        return text
    boundaries = list(_THINK_PATCH_BOUNDARY.finditer(text))
    if len(boundaries) != 1:
        return ""
    boundary = boundaries[0]
    prefix = text[: boundary.start()]
    # Only a standalone delimiter after plain reasoning can anchor a draft.
    if (
        not prefix.endswith("\n")
        or _OUTSIDE_TAG_LIKE.search(prefix)
        or _DRAFT_PREFIX_LITERAL.search(prefix)
        or _DRAFT_QUOTATION.search(prefix)
        or _EXAMPLE_CUE.search(prefix)
        or _FENCED_EXAMPLE_CAPTION.search(prefix)
    ):
        return ""
    return text[boundary.end() :]


def _missing_close_position(
    draft: str, body_start: int, next_tag: re.Match[str], value: str
) -> int | None:
    # A missing close is recoverable only after a complete inline value,
    # followed by whitespace and another register at the start of a line.
    inline = draft[body_start : next_tag.start()].partition("\n")[0]
    if (
        next_tag.group(1)
        or draft[next_tag.start() - 1] != "\n"
        or not value
        or inline.strip() != value
        or value.endswith(":")
        or _DRAFT_LITERAL_MARKER.search(value)
        or _EXAMPLE_CUE.search(value)
    ):
        return None
    return body_start + len(inline)


def _next_draft_boundary(
    draft: str, start: int, spans: list[tuple[int, int]]
) -> re.Match[str] | None:
    register = _next_unquoted_tag(_RESERVED_REGISTER_TAG, draft, start, spans)
    structural = _next_unquoted_tag(_DRAFT_TAG_TOKEN, draft, start, spans)
    if register is None or (
        structural is not None and structural.start() < register.start()
    ):
        return None
    return register


def _valid_draft_trailer(text: str) -> bool:
    return bool(_OPTIONAL_SUMMARY_TRAILER.fullmatch(text)) and not (
        _DRAFT_QUOTATION.search(text)
        or _EXAMPLE_CUE.search(text)
        or _DRAFT_FENCE.search(text)
    )


def _recover_register_draft(text: str) -> str:
    """Close only unambiguous register bodies in the final raw draft."""
    draft = _final_register_draft(text)
    spans = _register_literal_spans(draft, include_draft_fences=True)
    values: dict[str, str] = {}
    missing_closes: list[tuple[int, str]] = []
    cursor = end = 0
    while opening := _register_opening(draft, cursor):
        name = opening.group(2)
        if name in values or _is_literal(cursor, spans):
            return ""
        next_tag = _next_draft_boundary(draft, opening.end(), spans)
        if next_tag is None:
            return ""
        value = draft[opening.end() : next_tag.start()].strip()
        values[name] = value
        if next_tag.group(1) == "/" and next_tag.group(2) == name:
            end = next_tag.end()
            cursor = len(draft) - len(draft[end:].lstrip())
            continue
        position = _missing_close_position(
            draft, opening.end(), next_tag, value
        )
        if position is None:
            return ""
        missing_closes.append((position, f"</{name}>"))
        cursor = next_tag.start()
    if not missing_closes or not _valid_draft_trailer(draft[end:].strip()):
        return ""
    repaired = draft[:end]
    for position, tag in reversed(missing_closes):
        repaired = repaired[:position] + tag + repaired[position:]
    return repaired if extract_register_bodies(repaired) == values else ""


def should_attempt_memory_deltas_repair(
    raw_output: str,
    deltas: WorkingMemoryDeltas,
    *,
    min_chars_for_repair: int = 100,
) -> bool:
    """Check whether an unsuccessful direct parse has a recoverable draft."""
    del min_chars_for_repair  # Keep the legacy keyword for callers.
    if not isinstance(deltas, WorkingMemoryDeltas) or not deltas.is_empty():
        return False
    _, touched = parse_memory_deltas(raw_output)
    return not touched and bool(
        _recover_register_draft(str(raw_output or "").strip())
    )


def parse_memory_deltas_with_repair(
    raw_output: str,
    *,
    repair_fn: Callable[[str], str] | None = None,
    min_chars_for_repair: int = 100,
    full_replace: bool = False,
) -> tuple[WorkingMemoryDeltas, frozenset[str], bool, str]:
    """Parse directly or recover structure without changing register values."""
    del min_chars_for_repair  # Keep the legacy keyword for callers.
    raw = str(raw_output or "")
    direct, touched = parse_memory_deltas(raw, full_replace=full_replace)
    if touched:
        return direct, touched, False, raw
    repaired = _recover_register_draft(raw.strip())
    if not repaired:
        return direct, touched, False, raw
    expected, expected_touched = parse_memory_deltas(
        repaired, full_replace=full_replace
    )
    if repair_fn is not None:
        formatted = str(repair_fn(raw) or "")
        actual = parse_memory_deltas(formatted, full_replace=full_replace)
        if actual != (expected, expected_touched):
            return direct, touched, True, formatted
        repaired = formatted
    return expected, expected_touched, True, repaired


def apply_append_deltas(
    snapshot: WorkingMemorySnapshot,
    deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    *,
    touched: frozenset[str] | None = None,
) -> WorkingMemorySnapshot:
    delta_map: Mapping[str, str] = coerce_working_memory_deltas(
        deltas
    ).to_dict(skip_empty=False)
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
        merged[name] = (
            f"{base_text}\n\n{delta_text}" if base_text else delta_text
        )
    return _snapshot_from_mapping(merged)


def apply_replace_deltas(
    snapshot: WorkingMemorySnapshot,
    deltas: WorkingMemoryDeltas | Mapping[str, str] | None,
    *,
    touched: frozenset[str] | None = None,
    registers: frozenset[str] | None = None,
) -> WorkingMemorySnapshot:
    delta_map: Mapping[str, str] = coerce_working_memory_deltas(
        deltas
    ).to_dict(skip_empty=False)
    merged: Dict[str, str] = snapshot.to_dict()
    replace_names = (
        registers
        if registers is not None
        else frozenset(
            name
            for name in REGISTER_ORDER
            if REGISTER_MERGE_MODES.get(name) == "replace"
        )
    )
    for name in REGISTER_ORDER:
        if name not in replace_names or not _replace_applies(
            name, deltas, delta_map, touched
        ):
            continue
        merged[name] = _normalize_register_text(delta_map.get(name, ""))
    return _snapshot_from_mapping(merged)


def _replace_applies(
    name: str,
    deltas: Any,
    delta_map: Mapping[str, str],
    touched: frozenset[str] | None,
) -> bool:
    """Заменять ли регистр: по touched, по явному ключу в Mapping, иначе по непустому тексту."""
    if touched is not None:
        return name in touched
    if isinstance(deltas, Mapping):
        return name in deltas
    return bool(_normalize_register_text(delta_map.get(name)))


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
        append_all = coerce_working_memory_deltas(append_deltas).to_dict(
            skip_empty=False
        )
        for name in append_touched:
            combined[name] = append_all.get(name, "")
    if replace_touched:
        touched.update(replace_touched)
        replace_all = coerce_working_memory_deltas(replace_deltas).to_dict(
            skip_empty=False
        )
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


def render_session_memory_for_summary_message(
    snapshot: WorkingMemorySnapshot,
) -> str:
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
    "MEMORY_DELTAS_FORMAT_REPAIR_REQUEST_LABEL",
    "REGISTER_MERGE_MODES",
    "REGISTER_ORDER",
    "SUMMARY_PATCH_REPLACE_REGISTERS",
    "WorkingMemoryDeltas",
    "WorkingMemorySnapshot",
    "apply_append_deltas",
    "apply_memory_patch",
    "apply_replace_deltas",
    "apply_summary_memory_patch",
    "build_memory_deltas_format_repair_messages",
    "coerce_working_memory_deltas",
    "extract_register_bodies",
    "parse_memory_deltas",
    "parse_memory_deltas_with_repair",
    "read_snapshot_from_state",
    "render_persistent_state_for_prompt",
    "render_session_memory_for_summary_message",
    "resolve_persistent_state_block",
    "should_attempt_memory_deltas_repair",
]
