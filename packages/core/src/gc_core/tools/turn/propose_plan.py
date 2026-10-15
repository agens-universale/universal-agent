"""turn_propose_plan — тул режима plan: предложить готовый план и завершить ход.

Успех кладёт план в ``state.shared.plan`` (канон) и поднимает
``state.plan_proposed`` — по нему цикл агента закрывает ход отдельной веткой:
судья финала к плану не применяется, план — не ответ на запрос. Вне режима plan
и у субагента тул отвечает отказом.
"""

import json
from typing import Any, Dict

from gc_core.constants.tool_names import PROPOSE_PLAN_TOOL_NAME
from gc_core.plan_store import (
    PLAN_MAX_CHARS,
    PLAN_STATUS_REWORKING,
    new_plan_block,
    normalize_plan_block,
)
from gc_core.security.permission_mode import Mode, resolve_mode
from gc_core.tools.simple_tool_base import AbstractSimpleTool

#: Правило «план уже предложен» живёт в описании, а не в механике: отличить
#: дополнение той же задачи от новой может только модель. Описание уезжает в
#: схему на каждом запросе, поэтому носитель правила — оно.
_TOOL_DESCRIPTION = (
    "Finalizes turn: yes. "
    "Available ONLY in the 'plan' permission mode. "
    "Propose the finished implementation plan to the user for approval and end the turn. "
    "Call after research is complete; the user will approve, request rework, or cancel. "
    "Do NOT use for progress updates or questions (use turn_ask_human for questions). "
    "When a plan is already proposed and the user has not reviewed it yet, never replace it "
    "silently. A new request on the SAME task: propose an updated plan in full — the earlier "
    "steps plus the new ones, so nothing the user has not seen is lost. A request on "
    "SOMETHING ELSE: first ask via turn_ask_human whether to do the pending plan first, "
    "replace it, or merge both tasks into one plan, and propose only after they answer. "
    "When the user asks to SEE the current plan again or to approve it, do not retell it "
    "and do not claim it is approved: call this tool with show_current=true (and an empty "
    "plan) — the stored plan is shown again unchanged and the user gets the choice."
)


class ProposePlan(AbstractSimpleTool):
    # Тело исполняется в ядре: тул меняет только состояние сессии.
    effect_in_core = True

    @staticmethod
    def _result(
        *,
        ok: bool,
        status: str,
        message: str,
        error: Dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": PROPOSE_PLAN_TOOL_NAME,
                "ok": ok,
                "status": status,
                "message": message,
                "error": error,
            },
            ensure_ascii=False,
        )

    @classmethod
    def _rejected(cls, error_type: str, message: str, **fields: Any) -> str:
        return cls._result(
            ok=False,
            status="rejected",
            message=message,
            error={"type": error_type, "message": message, **fields},
        )

    @classmethod
    def tool_description(cls, settings: Any = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": PROPOSE_PLAN_TOOL_NAME,
            "description": _TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "string",
                        "description": (
                            "The complete plan in markdown: goal, ordered steps, "
                            "files to touch, verification. Self-contained — the user "
                            "approves exactly this text."
                        ),
                    },
                    "show_current": {
                        "type": "boolean",
                        "description": (
                            "true — show the plan already stored in the session "
                            "again, unchanged (pass an empty plan). Use when the "
                            "user asks to see or approve the current plan. Not "
                            "for a plan under rework: propose the updated plan "
                            "in full instead."
                        ),
                    },
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, settings: Any = None) -> str:
        return (
            f"Tool name: {PROPOSE_PLAN_TOOL_NAME}\n"
            "Finalizes turn: yes. Plan-mode only: propose the finished plan for user approval.\n"
            'Input schema: {"plan": "string (required, markdown)", '
            '"show_current": "boolean (optional)"}\n'
            "On approval the session continues in the mode the user chose; on rework "
            "the user's feedback arrives as the next message while plan mode stays on.\n"
            "A plan already proposed and not yet reviewed is never replaced silently: "
            "on the SAME task propose an updated plan in full (earlier steps plus the new "
            "ones); on SOMETHING ELSE ask via turn_ask_human first — do the pending plan, "
            "replace it, or merge both tasks into one plan.\n"
            "To show the current plan again or let the user approve it, call with "
            "show_current=true and an empty plan — never retell the plan and never "
            "say it is approved."
        )

    async def acall(
        self, plan: str = "", show_current: bool | None = None, **kwargs: Any
    ) -> str:
        if getattr(self._state, "parent_state_id", None) is not None:
            # План — договор корневого агента с человеком; субагент возвращает
            # находки текстом.
            return self._rejected(
                "not_available_to_subagent",
                "Subagents cannot propose a plan. Return your findings as text.",
            )
        if resolve_mode(self._ctx) is not Mode.PLAN:
            return self._rejected(
                "not_in_plan_mode",
                "Not in plan mode. Finish the turn normally via turn_finish.",
            )
        if show_current:
            return self._show_current()
        if not isinstance(plan, str) or not plan.strip():
            # Пустой план без явного флага — не «покажи прежний»: модель,
            # написавшая новый план прозой и приславшая сюда "", иначе подняла
            # бы выбор над СТАРЫМ текстом.
            return self._rejected(
                "invalid_argument",
                "plan must be a non-empty string.",
                field="plan",
            )
        if len(plan) > PLAN_MAX_CHARS:
            return self._rejected(
                "limit_exceeded",
                (
                    f"plan is too long ({len(plan)} chars, limit {PLAN_MAX_CHARS}). "
                    "Shorten the plan: keep the goal, the ordered steps and "
                    "verification; move details out."
                ),
                field="plan",
            )
        self._state.shared.plan = new_plan_block(plan)
        self._state.plan_proposed = True
        return self._result(
            ok=True,
            status="proposed",
            message="Plan proposed; the run ends here and waits for user approval.",
        )

    def _show_current(self) -> str:
        """Предъявить сохранённый план заново: текст, статус и дата не меняются."""
        plan = normalize_plan_block(getattr(self._state.shared, "plan", None))
        if plan is None:
            return self._rejected(
                "no_plan",
                "There is no stored plan to show. Propose a plan first.",
            )
        if plan["status"] == PLAN_STATUS_REWORKING:
            return self._rejected(
                "plan_under_rework",
                "The plan is under rework. Propose the updated plan in full.",
            )
        self._state.plan_proposed = True
        return self._result(
            ok=True,
            status=plan["status"],
            message=(
                "The stored plan is shown to the user again, unchanged; the run "
                "ends here and waits for their decision."
            ),
        )
