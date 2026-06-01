import json
from typing import Any, Dict, Optional

from config_modules.working_memory import (
    DEFAULT_MAX_CHARS_CONSTRAINTS as _DEFAULT_MAX_CHARS_CONSTRAINTS,
    DEFAULT_MAX_CHARS_GOAL as _DEFAULT_MAX_CHARS_GOAL,
    DEFAULT_MAX_CHARS_KNOWLEDGE as _DEFAULT_MAX_CHARS_KNOWLEDGE,
    DEFAULT_MAX_CHARS_STATE as _DEFAULT_MAX_CHARS_STATE,
    DEFAULT_TOTAL_BUDGET_CHARS as _DEFAULT_TOTAL_BUDGET_CHARS,
    working_memory_limits_from_params,
)
from tools.simple_tool_base import AbstractSimpleTool

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


class WorkingMemory(AbstractSimpleTool):
    tool_name = "working_memory"

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
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "working_memory",
            "description": (
                "Thread/session memory for THIS dialogue only — not project-wide 'remember forever'. "
                "Do not append on every trivial step; write after meaningful progress or before handoff/delegation. "
                "This tool does NOT write files to disk. "
                "Stores compact state that survives dialog summarization. "
                "Use fixed registers only: "
                "goal = user intent, desired outcome, success criteria, definition of done; "
                "constraints = hard requirements, preferences, prohibitions, format/technical/safety limits; "
                "state = current plan, progress, completed steps, open TODOs, blockers, next actions, active hypotheses; "
                "knowledge = stable verified facts, architecture decisions, implementation notes, artifact references, evidence. "
                "Actions: list, read, append, rewrite. "
                "Use append for compact new deltas only. Use rewrite for compaction, cleanup, deduplication, or removing obsolete information. "
                "Do NOT store chain-of-thought, verbose reasoning, raw logs, large blobs, full file contents, repetitive history, or transient chatter. "
                "Register selection: objective/done criteria -> goal; requirements/preferences -> constraints; progress/TODO/blocker/next action -> state; stable fact/decision/artifact/evidence -> knowledge. "
                "If unsure: actionable unresolved information -> state; stable reusable information -> knowledge; requirement/preference -> constraints; desired outcome -> goal. "
                "Keep memory compact, structured, and durable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "append", "rewrite"],
                    },
                    "register": {
                        "type": "string",
                        "enum": list(REGISTER_ORDER),
                        "description": "Target memory register.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Brief justification for why this update is useful and "
                            "why it belongs in this register. Think before writing memory."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown content for append or rewrite.",
                    },
                },
                "required": ["action"],
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
            f"Use `register` ({registers}) with `action` append/rewrite/read/list.\n"
            "To create files on the filesystem, use the filesystem write tool with "
            "`path` (absolute) and `content`.\n"
        )

    @classmethod
    def prompt_description(cls) -> str:
        return """Tool name: working_memory
Tool description: Persistent per-thread operational memory for the agent.
This tool does NOT write files to disk or filesystem.
It stores compact durable task state that survives dialog summarization.

Registers:
- goal: user intent, desired outcome, success criteria, definition of done
- constraints: hard requirements, preferences, prohibitions, format/technical/safety limits
- state: current plan, progress, completed steps, open TODOs, blockers, next actions, active hypotheses
- knowledge: stable verified facts, architecture decisions, implementation notes, artifact references, evidence

Actions:
- list: show register sizes
- read: return one register
- append: add compact new deltas only
- rewrite: fully replace a register for compaction, cleanup, deduplication, or removing obsolete information

Register selection:
- objective/done criteria -> goal
- requirements/preferences -> constraints
- progress/TODO/blocker/next action -> state
- stable fact/decision/artifact/evidence -> knowledge

If unsure:
- actionable unresolved information -> state
- stable reusable information -> knowledge
- requirement/preference -> constraints
- desired outcome -> goal

Do NOT store chain-of-thought, verbose reasoning, raw logs, large blobs, full file contents, repetitive history, or transient chatter.
Keep memory compact, structured, and durable.

Input schema: {"type":"object","properties":{"action":{"type":"string","enum":["list","read","append","rewrite"]},"register":{"type":"string","enum":["goal","constraints","state","knowledge"]},"reason":{"type":"string"},"content":{"type":"string"}},"required":["action"],"additionalProperties":false}
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
            "tool": "working_memory",
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

    def __call__(
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
            "rewrite": self._action_rewrite,
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
        if not isinstance(reason, str) or not reason.strip():
            return self._error(
                action=action,
                error_type="invalid_reason",
                message=f"reason must be non-empty for {action}.",
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

    def _action_rewrite(self, register: str, reason: str, content: str, **_: Any) -> str:
        register_err = self._validate_register(register, "rewrite")
        if register_err:
            return register_err
        validation_err = self._validate_reason_and_content("rewrite", reason, content)
        if validation_err:
            return validation_err

        registers = self._get_registers()
        new_text = content.strip()
        limits_err = self._check_limits(
            action="rewrite",
            registers=registers,
            target_register=register,
            new_text=new_text,
        )
        if limits_err:
            return limits_err

        registers[register] = new_text
        self._set_registers(registers)
        return self._result(ok=True, action="rewrite", registers=registers, error=None)

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
                "Read and rewrite it compactly first."
                if action == "append"
                else "Provide a more compact rewrite."
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
                    "registers with rewrite first."
                ),
            )
        return None


__all__ = ["WorkingMemory"]
