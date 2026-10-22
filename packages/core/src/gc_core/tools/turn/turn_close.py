"""Правило конца хода: ответ модели, отчёт ``turn_finish`` и план.

Единое для всех фронтендов: ядро решает, что показать ответом, что — итогом
или заметкой; фронтенд только печатает :meth:`TurnClose.render`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Sequence

from gc_core.constants.tool_names import (
    FINISH_TOOL_NAME,
    PROPOSE_PLAN_TOOL_NAME,
)
from gc_core.message_projection import project_assistant_text
from gc_core.tools.turn.propose_plan import ProposePlan
from gc_core.tools.turn.submit_solution import SubmitSolution, TurnFinishView

LABEL_SUMMARY = "итог"
LABEL_NOTE = "заметка"

_CLOSING_TOOLS = (FINISH_TOOL_NAME, PROPOSE_PLAN_TOOL_NAME)


@dataclass(frozen=True)
class TurnClose:
    """Как закрыть ход в ленте.

    ``reply`` — текст модели ответа, закрывшего ход (``""`` — нет);
    ``body`` — отчёт или план, показываемый как ответ (``""`` — отчёт ушёл
    в итог); ``label`` — ``LABEL_SUMMARY`` / ``LABEL_NOTE`` / ``""``;
    ``note`` — отчёт под меткой «итог» (``""`` — нет); ``comment`` —
    оговорка под меткой: после отчёта или одна.
    """

    reply: str
    body: str
    label: str
    note: str
    comment: str

    def _record(self, markdown: bool) -> str:
        if self.label == "":
            return ""
        # Метка — своей строкой, текст — абзацами пункта с отступом:
        # отчёт, начатый или кончившийся блоком кода, таблицей или
        # списком, не сливается ни с меткой, ни с оговоркой.
        label = f"**{self.label}:**" if markdown else f"{self.label}:"
        text = "\n\n".join(
            part for part in (self.note, self.comment) if part != ""
        )
        lines = [f"  {ln}" if ln else "" for ln in text.split("\n")]
        return f"* {label}\n\n" + "\n".join(lines)

    def render(
        self, *, markdown: bool, after_shown_reply: bool = False
    ) -> str:
        """Текст конца хода.

        При ``after_shown_reply`` ответ модели уже показан: он не
        повторяется; разделитель перед остальным ставит вызывающий.
        """
        skip_reply = after_shown_reply and self.reply != ""
        parts = [
            self.body,
            self._record(markdown),
        ]
        if not skip_reply:
            parts.insert(0, self.reply)
        return "\n\n".join(part for part in parts if part != "")


def finish_close(view: TurnFinishView, reply: str) -> TurnClose:
    """Правило ``turn_finish``: ответ, отчёт и заметка без дублей."""
    shown = reply.strip()
    report = view.report
    comment = view.comment
    label_note = LABEL_NOTE if comment else ""
    if shown == "":
        return TurnClose("", report, label_note, "", comment)
    if report in ("", shown):
        return TurnClose(shown, "", label_note, "", comment)
    return TurnClose(shown, "", LABEL_SUMMARY, report, comment)


def without_repeated_body(close: TurnClose, shown: str) -> TurnClose:
    """Отчёт, равный ответу, уже показанному в этом ходе, не печатается
    вторым блоком; запись (заметка) остаётся.

    ``shown`` — что фронтенд уже показал ответом; сравнение после strip.
    """
    if close.reply != "" or close.body.strip() != shown.strip():
        return close
    return replace(close, body="")


def plan_close(plan: str, reply: str) -> TurnClose:
    """План после ответа модели; равный плану ответ не дублируется."""
    shown = reply.strip()
    if shown == plan.strip():
        return TurnClose(plan, "", "", "", "")
    return TurnClose(shown, plan, "", "", "")


def close_view(result: Any, reply: str) -> TurnClose | None:
    """Конец хода по итогу ``run()``; проза и прочее — ``None``."""
    plan = ProposePlan.present_result(result)
    if plan is not None:
        return plan_close(plan, reply)
    view = SubmitSolution.finish_view(result)
    if view is not None:
        return finish_close(view, reply)
    return None


def _message_text(item: Any, tags: Sequence[str]) -> str:
    return project_assistant_text(
        item.get("content"), tags=tags, strip_think=True
    ).strip()


def _is_assistant_message(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "assistant"
    )


def assistant_reply_text(
    items: Sequence[Any], *, tags: Sequence[str] = ()
) -> str:
    """Проза модели из записей: все ответы ``assistant`` через пустую
    строку, ``<think>`` отброшен."""
    parts = [
        _message_text(item, tags)
        for item in items
        if _is_assistant_message(item)
    ]
    return "\n\n".join(part for part in parts if part).strip()


def starts_new_response(item: Any) -> bool:
    """Запись открывает новый ответ модели: результат вызова или реплика
    пользователя."""
    if not isinstance(item, dict):
        return False
    if item.get("type") == "function_call_output":
        return True
    # user-записи в треде лежат без type (в том числе technical-bounce).
    return item.get("type") in (None, "message") and item.get("role") == "user"


def closing_replies(
    items: Iterable[Any], *, tags: Sequence[str] = ()
) -> dict[str, str]:
    """``call_id`` закрывающего вызова → текст ответа модели при нём.

    Текст копится по ``assistant``-сообщениям и сбрасывается на результат
    вызова и на сообщение пользователя. Повтор ``call_id`` непустой текст
    не затирает.
    """
    out: dict[str, str] = {}
    texts: list[str] = []
    for item in items:
        if starts_new_response(item):
            texts = []
        elif _is_assistant_message(item):
            texts.append(_message_text(item, tags))
        elif (
            isinstance(item, dict)
            and item.get("type") == "function_call"
            and item.get("name") in _CLOSING_TOOLS
            and item.get("call_id")
        ):
            joined = "\n\n".join(text for text in texts if text).strip()
            # Копия пары вызов/результат после компакции идёт без ответа
            # модели: она не затирает уже найденный текст.
            if not out.get(item["call_id"]):
                out[item["call_id"]] = joined
    return out


def closing_reply_of(agent: Any) -> str:
    """Ответ модели, закрывшего последний ход агента; ``""`` — нет."""
    try:
        executor = getattr(agent, "executor", None)
    except RuntimeError:
        return ""
    reply = getattr(executor, "last_closing_reply", "")
    return reply if isinstance(reply, str) else ""
