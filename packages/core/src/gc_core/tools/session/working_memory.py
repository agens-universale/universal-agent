"""Thread session registers (goal/constraints/state/knowledge).

Naming: agent-visible tool ``session_memory``; storage ``state.shared.working_memory``
and yaml ``working_memory:`` limits — unchanged for saved-thread compatibility.
"""

import json
from typing import Any, Dict, Optional

from gc_core.config_modules.working_memory import DEFAULT_MAX_CHARS_CONSTRAINTS as _DEFAULT_MAX_CHARS_CONSTRAINTS, DEFAULT_MAX_CHARS_GOAL as _DEFAULT_MAX_CHARS_GOAL, DEFAULT_MAX_CHARS_KNOWLEDGE as _DEFAULT_MAX_CHARS_KNOWLEDGE, DEFAULT_MAX_CHARS_STATE as _DEFAULT_MAX_CHARS_STATE, DEFAULT_TOTAL_BUDGET_CHARS as _DEFAULT_TOTAL_BUDGET_CHARS, working_memory_limits_from_params
from gc_core.constants.tool_names import SESSION_MEMORY_TOOL_NAME
from gc_core.loop_detector.constants import GENERIC_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool

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
REGISTER_SET = frozenset(REGISTER_ORDER)

DEFAULT_REGISTER_LIMITS = {
    REGISTER_GOAL: _DEFAULT_MAX_CHARS_GOAL,
    REGISTER_CONSTRAINTS: _DEFAULT_MAX_CHARS_CONSTRAINTS,
    REGISTER_STATE: _DEFAULT_MAX_CHARS_STATE,
    REGISTER_KNOWLEDGE: _DEFAULT_MAX_CHARS_KNOWLEDGE,
}
DEFAULT_TOTAL_BUDGET_CHARS = _DEFAULT_TOTAL_BUDGET_CHARS


def _registers_summary(
    registers: Dict[str, str],
    limits: Dict[str, int],
) -> Dict[str, str]:
    return {
        name: (
            f"Length - {len(registers.get(name, ''))} chars "
            f"(limit: {limits.get(name, 0)} chars)"
        )
        for name in REGISTER_ORDER
    }


def _total_chars(registers: Dict[str, str]) -> int:
    return sum(len(registers.get(name, "")) for name in REGISTER_ORDER)


@loop_warn_on_repeat(**GENERIC_PROFILE)
class WorkingMemory(AbstractSimpleTool):
    tool_name = SESSION_MEMORY_TOOL_NAME

    def __init__(self, ctx: Any = None, state: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state
        cfg = getattr(ctx, "config_dict", None) or {}
        limits_cfg = (
            working_memory_limits_from_params(cfg.get("working_memory_params")) or {}
        )

        self._register_limits = {
            REGISTER_GOAL: self._cfg_int(
                limits_cfg, "max_chars_goal", DEFAULT_REGISTER_LIMITS[REGISTER_GOAL]
            ),
            REGISTER_CONSTRAINTS: self._cfg_int(
                limits_cfg,
                "max_chars_constraints",
                DEFAULT_REGISTER_LIMITS[REGISTER_CONSTRAINTS],
            ),
            REGISTER_STATE: self._cfg_int(
                limits_cfg, "max_chars_state", DEFAULT_REGISTER_LIMITS[REGISTER_STATE]
            ),
            REGISTER_KNOWLEDGE: self._cfg_int(
                limits_cfg,
                "max_chars_knowledge",
                DEFAULT_REGISTER_LIMITS[REGISTER_KNOWLEDGE],
            ),
        }
        self._total_budget_chars = self._cfg_int(
            limits_cfg, "total_budget_chars", DEFAULT_TOTAL_BUDGET_CHARS
        )

    @staticmethod
    def _cfg_int(cfg: Dict[str, Any], key: str, default: int) -> int:
        value = cfg.get(key)
        if isinstance(value, int) and value > 0:
            return value
        return default

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "session_memory",
            "description": (
                "Thread/session memory for THIS dialogue only — not project-wide 'remember forever'. "
                "Do not append on every trivial step; update after meaningful progress or before handoff/delegation. "
                "This tool does NOT write files to disk. "
                "Stores compact state that survives dialog summarization. "
                "Use fixed registers only: "
                "goal = user intent, desired outcome, success criteria, definition of done; "
                "constraints = hard requirements, preferences, prohibitions, format/technical/safety limits; "
                "state = blockers, active hypotheses, findings in progress, decisions still "
                "open — narrative working state. The step-by-step plan with statuses belongs "
                "in the session_todo checklist tool when it is available; if that tool is NOT "
                "available, keep the plan here instead. Never keep the same steps in both "
                "places; "
                "knowledge = stable verified facts, architecture decisions, implementation notes, artifact references, evidence. "
                "Actions: list, read, append (short delta), write (full replace of one register; rare). "
                "Every response includes per-register length vs limit in registers. "
                "Use append for compact new deltas only. Use write for full replace: compaction, cleanup, deduplication, or removing obsolete information. "
                "Maintenance: when register sizes in that metadata approach or exceed limits, proactively compact the affected register—read it, then write the compact full replacement (deduplicate, drop obsolete detail; do not keep appending into an overfull register). "
                "Move bulky details to a file/artifact when needed; keep a concise form of what belongs in that register per the definitions above (e.g. facts, decisions, blockers, artifact links—not chain-of-thought or raw dumps). "
                "Do not wait for the user to ask; memory hygiene is part of using this tool. "
                "Do NOT store chain-of-thought, verbose reasoning, raw logs, large blobs, full file contents, repetitive history, or transient chatter. "
                "Register selection: objective/done criteria -> goal; requirements/preferences -> constraints; blocker/hypothesis/open decision -> state; stable fact/decision/artifact/evidence -> knowledge; step-by-step plan and step statuses -> session_todo tool when available, otherwise -> state. "
                "If unsure: an actionable STEP of the task -> session_todo (or state if that tool is absent); other unresolved information -> state; stable reusable information -> knowledge; requirement/preference -> constraints; desired outcome -> goal. "
                "Keep memory compact, structured, and durable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "append", "write"],
                        "description": (
                            "list = show register sizes; "
                            "read = return one register; "
                            "append = add ONLY the new delta; "
                            "write = fully replace one register (rare; full overwrite)."
                        ),
                    },
                    "register": {
                        "type": "string",
                        "enum": list(REGISTER_ORDER),
                        "description": (
                            "Target memory register. Required for every action "
                            "(ignored for list)."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Optional brief justification for why this update is useful "
                            "and why it belongs in this register. Recommended but optional; "
                            "content is required for append/write."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown text for append/write. "
                            "For append: ONLY the new delta. "
                            "For write: the FULL updated register content (full overwrite)."
                        ),
                    },
                },
                "required": ["action", "register"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def wrong_arguments_feedback_hint(
        cls,
        *,
        received_args: str,
        parsed_args: dict[str, Any] | None,
        _message: str,
    ) -> str | None:
        args = parsed_args
        if args is None and received_args:
            try:
                args = json.loads(received_args)
            except json.JSONDecodeError:
                return None
        if not isinstance(args, dict) or "path" not in args:
            return None
        registers = ", ".join(REGISTER_ORDER)
        return (
            "💡 Hint: This tool is for INTERNAL AGENT MEMORY only — it does NOT write files to disk.\n"
            f"Use `register` ({registers}) with `action` append/write/read/list.\n"
            "To create files on the filesystem, use the filesystem write tool with "
            "`path` (absolute) and `content`.\n"
        )

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        return """Tool name: session_memory
Tool description: Thread/session memory for THIS dialogue only — not project-wide remember-forever.
Persistent per-thread operational memory for the agent.
Do not append on every trivial step; update after meaningful progress or before handoff/delegation.
This tool does NOT write files to disk or filesystem.
It stores compact durable task state that survives dialog summarization.

Registers:
- goal: user intent, desired outcome, success criteria, definition of done
- constraints: hard requirements, preferences, prohibitions, format/technical/safety limits
- state: blockers, active hypotheses, findings in progress, decisions still open — narrative working state. The step-by-step plan with statuses belongs in the session_todo checklist tool when it is available; if that tool is NOT available, keep the plan here instead. Never keep the same steps in both places
- knowledge: stable verified facts, architecture decisions, implementation notes, artifact references, evidence (file refs: full absolute paths as in tools, e.g. `/home/.../src/backends/local.py` — never basename-only)

Actions:
- list: show register sizes
- read: return one register
- append: add compact new deltas only
- write: fully replace one register (full overwrite; rare) — for compaction, cleanup, deduplication, or removing obsolete information

Maintenance (register hygiene):
- Every tool response includes per-register length vs limit in `registers`.
- When sizes approach or exceed limits, read the affected register and write a compact full replacement (deduplicate, drop obsolete detail; do not keep appending into an overfull register).
- Move bulky details to a file/artifact when needed; keep a concise form of register-appropriate content per definitions above (facts, decisions, blockers, artifact links—not chain-of-thought or raw dumps).
- Do not wait for the user to ask; memory hygiene is part of using this tool.

Register selection:
- objective/done criteria -> goal
- requirements/preferences -> constraints
- blocker/hypothesis/open decision -> state
- step-by-step plan and step statuses -> session_todo tool when available, otherwise -> state
- stable fact/decision/artifact/evidence -> knowledge

If unsure:
- an actionable STEP of the task -> session_todo (or state if that tool is absent); other unresolved information -> state
- stable reusable information -> knowledge
- requirement/preference -> constraints
- desired outcome -> goal

Do NOT store chain-of-thought, verbose reasoning, raw logs, large blobs, full file contents, repetitive history, or transient chatter.
Keep memory compact, structured, and durable.

`register` is required for every action (ignored for list).
`reason` is optional; `content` is required for append/write.

Input schema: {"type":"object","properties":{"action":{"type":"string","enum":["list","read","append","write"]},"register":{"type":"string","enum":["goal","constraints","state","knowledge"]},"reason":{"type":"string"},"content":{"type":"string"}},"required":["action","register"],"additionalProperties":false}
"""

    def _get_registers(self) -> Dict[str, str]:
        shared = getattr(self._state, "shared", None)
        if shared is None:
            return {}
        raw = getattr(shared, "working_memory", None)
        if not isinstance(raw, dict):
            return {}
        return {
            name: str(raw.get(name, "")) if isinstance(raw.get(name, ""), str) else ""
            for name in REGISTER_ORDER
        }

    def _set_registers(self, registers: Dict[str, str]) -> None:
        self._state.shared.working_memory = {
            name: registers.get(name, "") for name in REGISTER_ORDER
        }

    def _result(
        self,
        *,
        ok: bool,
        action: str,
        registers: Optional[Dict[str, str]] = None,
        total_chars: Optional[int] = None,
        content: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        current = registers if registers is not None else self._get_registers()
        payload: Dict[str, Any] = {
            "tool": WorkingMemory.registered_tool_name(),
            "ok": ok,
            "action": action,
            "error": error,
            "registers": _registers_summary(current, self._register_limits),
            "total_chars": _total_chars(current) if total_chars is None else total_chars,
        }
        if content is not None:
            payload["content"] = content
        return json.dumps(payload, ensure_ascii=False)

    def _error(self, *, action: str, error_type: str, message: str) -> str:
        registers = self._get_registers()
        return self._result(
            ok=False,
            action=action,
            registers=registers,
            total_chars=_total_chars(registers),
            error={"type": error_type, "message": message},
        )


    async def acall(
        self,
        action: str,
        register: str = "",
        reason: str = "",
        content: str = "",
        **kwargs: Any,
    ) -> str:
        if self._state is None:
            return self._result(
                ok=False,
                action=action,
                registers={},
                total_chars=0,
                error={"type": "no_state", "message": "state is missing."},
            )
        if kwargs:
            return self._error(
                action=action,
                error_type="unexpected_arguments",
                message=f"Unexpected arguments: {', '.join(sorted(kwargs.keys()))}.",
            )

        actions = {
            "list": self._action_list,
            "read": self._action_read,
            "append": self._action_append,
            "write": self._action_write,
        }
        handler = actions.get(action)
        if handler is None:
            return self._error(
                action=action,
                error_type="invalid_action",
                message=f"Unknown action: {action}",
            )
        return handler(register=register, reason=reason, content=content)

    def _validate_register(self, register: str, action: str) -> Optional[str]:
        if not isinstance(register, str) or not register.strip():
            return self._error(
                action=action,
                error_type="missing_register",
                message=(
                    f"register is required for {action}; "
                    "one of: goal, constraints, state, knowledge."
                ),
            )
        if register not in REGISTER_SET:
            return self._error(
                action=action,
                error_type="invalid_register",
                message=(
                    "register must be one of: goal, constraints, state, knowledge."
                ),
            )
        return None

    def _validate_reason_and_content(self, action: str, reason: str, content: str) -> Optional[str]:
        if reason is not None and not isinstance(reason, str):
            return self._error(
                action=action,
                error_type="invalid_reason",
                message=f"reason must be a string for {action}.",
            )
        if not isinstance(content, str) or not content.strip():
            return self._error(
                action=action,
                error_type="empty_content",
                message=f"content must be non-empty for {action}.",
            )
        return None

    def _action_list(self, **_: Any) -> str:
        registers = self._get_registers()
        return self._result(
            ok=True,
            action="list",
            registers=registers,
            total_chars=_total_chars(registers),
            error=None,
        )

    def _action_read(self, register: str, **_: Any) -> str:
        register_err = self._validate_register(register, "read")
        if register_err:
            return register_err

        registers = self._get_registers()
        return self._result(
            ok=True,
            action="read",
            registers=registers,
            total_chars=_total_chars(registers),
            content=registers.get(register, ""),
            error=None,
        )

    def _action_append(self, register: str, reason: str, content: str, **_: Any) -> str:
        register_err = self._validate_register(register, "append")
        if register_err:
            return register_err
        validation_err = self._validate_reason_and_content("append", reason, content)
        if validation_err:
            return validation_err

        registers = self._get_registers()
        existing = registers.get(register, "").strip()
        delta = content.strip()
        new_text = f"{existing}\n\n{delta}" if existing else delta

        limits_err = self._check_limits(
            action="append",
            registers=registers,
            target_register=register,
            new_text=new_text,
        )
        if limits_err:
            return limits_err

        registers[register] = new_text
        self._set_registers(registers)
        return self._result(ok=True, action="append", registers=registers, error=None)

    def _action_write(self, register: str, reason: str, content: str, **_: Any) -> str:
        register_err = self._validate_register(register, "write")
        if register_err:
            return register_err
        validation_err = self._validate_reason_and_content("write", reason, content)
        if validation_err:
            return validation_err

        registers = self._get_registers()
        new_text = content.strip()
        limits_err = self._check_limits(
            action="write",
            registers=registers,
            target_register=register,
            new_text=new_text,
        )
        if limits_err:
            return limits_err

        registers[register] = new_text
        self._set_registers(registers)
        return self._result(ok=True, action="write", registers=registers, error=None)

    def _check_limits(
        self,
        *,
        action: str,
        registers: Dict[str, str],
        target_register: str,
        new_text: str,
    ) -> Optional[str]:
        register_limit = self._register_limits[target_register]
        if len(new_text) > register_limit:
            hint = (
                "Read it, then action=write with a compact full replacement."
                if action == "append"
                else "Provide a more compact full replacement via action=write."
            )
            return self._error(
                action=action,
                error_type="register_limit_exceeded",
                message=(
                    f"Register '{target_register}' would exceed {register_limit} chars. "
                    f"{hint}"
                ),
            )

        projected_total = (
            sum(len(v) for k, v in registers.items() if k != target_register) + len(new_text)
        )
        if projected_total > self._total_budget_chars:
            return self._error(
                action=action,
                error_type="total_budget_exceeded",
                message=(
                    f"Writing to '{target_register}' would exceed total budget of "
                    f"{self._total_budget_chars} chars. Compact the largest relevant "
                    "registers with action=write first."
                ),
            )
        return None


__all__ = ["WorkingMemory"]
