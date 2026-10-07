import inspect
import json
import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional

from gc_core.attachments.model import Attachment, chip_of
from gc_core.attachments.store import commit_to_run_thread
from gc_core.execution.human import HumanRequestInterrupted
from gc_core.execution.ports import port as runtime_port
from gc_core.tools.simple_tool_base import AbstractSimpleTool

_logger = logging.getLogger(__name__)

# Shown on every successful ask_human observation (agent-visible JSON).
_RESPONSE_HINT = (
    "Classify the reply before advancing. "
    "Direct answer (specific choice or instruction) → use it and proceed. "
    "Delegation (user leaves the call to you: 'you decide', 'your call', "
    "'как считаешь', 'на твоё усмотрение') → do not decide silently. "
    "Investigate/weigh with other tools, then turn_ask_human with your proposed choice, "
    "reasoning, and explicit confirmation; proceed only after they agree — not a black box. "
    "Meta-question (user asks you back, e.g. 'what do you recommend?') → "
    "do not skip to the next task step. If you need facts, investigate with other tools "
    "first; reflect, then turn_ask_human in one turn with your answer, reasoning, and "
    "ask whether they agree or want to settle the original choice — not a snap reply. "
    "Ambiguous, empty, or off-topic → turn_ask_human again to clarify. "
    "If still insufficient and the user did not delegate, keep asking — do not guess."
)

_TOOL_DESCRIPTION = (
    "Finalizes turn: no. "
    "Request user input and wait for their reply. "
    "Use to ask for a choice, clarification, missing data, or explicit confirmation before risky or irreversible actions. "
    "Do NOT use for one-way status or explanations. "
    "Do NOT use as the final report of a completed task or to end the current user turn. "
    "Be clear and include necessary context; avoid vague questions. "
    "The `question` must be concise: the actual question plus minimal context only. "
    "Do not put long comparisons or background dumps into `question`; "
    "put detailed material in a separate artifact and reference it briefly here. "
    "When a finite choice set fits, prefer explicit options (A/B/C, numbered list, yes/no). "
    "After each call, follow the hint field in the tool result to classify and act on the reply."
)


@dataclass(frozen=True)
class HumanAnswer:
    """Ответ человека с изображениями (спека §20.3).

    Порт ``ctx.ask_human`` возвращает строку (прежнее поведение: встраивания SDK
    не ломаются) или ``HumanAnswer``. ``attachments`` — вложения из кармана
    ``ImageStore`` как есть: в тред их копирует ядро, модели они уходят
    техническим сообщением после батча.
    """

    text: str
    attachments: tuple[Attachment, ...] = ()


def _answer_text(answer: HumanAnswer) -> str:
    """Текст ответа, в котором названа каждая картинка ответа.

    TUI присылает текст уже с плашками; встраивающий SDK может прислать голый
    текст — тогда недостающие плашки встают в начало, иначе модель и лента не
    свяжут картинку с ответом.
    """
    missing = [
        chip_of(att)
        for att in answer.attachments
        if chip_of(att) not in answer.text
    ]
    return " ".join([*missing, answer.text.strip()]).strip()


async def _aread_human_response(
    ctx: "Any", question: str
) -> "str | HumanAnswer":
    """Спросить человека через порт вызывающего, а по умолчанию — через stdin.

    Ядро — библиотека: у встраивающего приложения свой способ спросить
    пользователя (веб-форма, чат, очередь), и ``input()`` там не просто
    неуместен, а невозможен — stdin принадлежит не ему. Порт задаётся полем
    ``ctx.ask_human``: обычная функция или корутинная, принимаются обе.

    Корутинный порт ждётся НА LOOP ВЫЗЫВАЮЩЕГО. Снятый синхронный двойник
    гонял его через ``run_coro_blocking``, то есть на одноразовом loop в
    одноразовом потоке: всё, что порт держит привязанным к своему loop —
    очередь, соединение, сессия, — там просто не работало.
    """
    native_port = runtime_port(ctx, "human_request")
    if callable(native_port):
        from gc_core.execution.human import HumanRequest

        result = await native_port(
            HumanRequest(kind="ask_human", question=question)
        )
        return (str(result) if result is not None else "").strip()
    port = getattr(ctx, "ask_human", None)
    if callable(port):
        result = port(question)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, HumanAnswer):
            return result  # картинки ответа: str() их потерял бы
        return (str(result) if result is not None else "").strip()
    # Порта нет — читаем stdin, как CLI. Это блокирующий ввод, и нативной
    # асинхронной формы у него нет; уводим в поток, чтобы не морозить loop
    # приложения на всё время, пока человек печатает.
    import asyncio

    return await asyncio.to_thread(_read_stdin_lines)


def _read_stdin_lines() -> str:
    """Построчное чтение stdin до пустой строки или EOF."""
    lines: list[str] = []
    while True:
        try:
            line = input()
            if line == "":
                break
            lines.append(line)
        except EOFError:
            break
    return "\n".join(lines).strip()


class AskHuman(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        question: str,
        response: str,
        error: Optional[Dict[str, Any]] = None,
        hint: Optional[str] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": AskHuman.registered_tool_name(),
            "ok": ok,
            "error": error,
            "question": question,
            "response": response,
        }
        if hint:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False)

    def _run_logger(self) -> logging.Logger:
        """Prefer the per-run logger from RunContext; fall back to the module logger."""
        ctx = getattr(self, "_ctx", None)
        logger = getattr(ctx, "logger", None) if ctx is not None else None
        return logger or _logger

    """
    Tool that allows the AI agent to ask a question and receive input from a human user.
    This is essential for clarifying ambiguous requirements, confirming decisions, or obtaining subjective feedback.
    """

    @classmethod
    def tool_description(cls, settings: Any = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "turn_ask_human",
            "description": _TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "Concise question to the human (markdown ok): what you need from them, "
                            "minimal context, and explicit options when choices are known. "
                            "Not for long content — use an artifact + short pointer here."
                        ),
                    }
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, settings: Any = None) -> str:
        # Legacy channel (tools_descr); not wired to API today — reserved for a future
        # tool_help-style meta-tool. Agent-facing spec: tool_description + result hint.
        description = """Tool name: turn_ask_human

Overview:
Finalizes turn: no. Ask the human user and wait for a free-form textual reply. Use for clarification,
confirmation, subjective judgment, design discussion, or continuing a dialogue.

What the agent actually sees (today):
- **tool_description** (function spec in the tools list) — when and how to ask; points to the result hint.
- **hint** field on each successful tool result — how to classify and handle the user's reply (canonical at runtime).
- **prompt_description** (this document) — full reference for tools_descr / a future tool_help meta-tool; not injected into the API prompt today.

Input schema:
{"type": "object", "properties": {"question": {"type": "string", "description": "Clear message to the human (markdown ok): question, context, preferably explicit answer options when choices are known."}}, "required": ["question"], "additionalProperties": false}

When to use:
- You cannot proceed confidently without human input.
- You need confirmation before a consequential action.
- The user is in a back-and-forth (brainstorming, planning, debugging) and you need their next turn.

When NOT to use:
- Routine work other tools can do (read files, search, run commands, fetch URLs).
- Guessing facts you could look up first — investigate, then ask if still unclear.

How to formulate `question`:
- Be clear; include necessary context; avoid vague prompts like "What should I do?".
- Keep it **short**: only the question and minimal context needed to answer.
- Long material belongs in a **user-facing artifact** (when your environment allows); in `question`, point to it and ask what you need—do not paste the full text here.
- When a finite choice set fits, prefer **closed-form** with explicit options (A/B/C, numbered list, yes/no) to minimize follow-up.
- Use an **open** question when options would be incomplete, misleading, or worse than free-form.
- Markdown is fine (code snippets, lists), but avoid long narratives. Hard limit: 4096 characters per question.

After each call — read `response` and the result **`hint`**:
Classify the reply before advancing. Same rules as the live hint; four categories:

1. **Direct answer** — substantive choice or instruction (e.g. "A", "yes", "use approach X"). Use it and proceed.

2. **Delegation to you** — user defers (e.g. "you decide", "your call", "как считаешь", "на твоё усмотрение").
   **Not** permission to act silently. Investigate/weigh with other tools, then `turn_ask_human` with your **proposed** choice, tradeoffs, and explicit confirmation. Proceed only after they agree — not a black box.

3. **Meta-question or follow-up** — user asks YOU back ("what do you recommend?", "explain X", trailing "?").
   Do **not** skip to the next methodology step. If facts are needed, use other tools first; reflect; then one `turn_ask_human` turn with your answer, reasoning, and ask to close the original choice — not a snap reply. Pacing follows the user.

4. **Ambiguous / empty / off-topic** — `turn_ask_human` again to clarify (e.g. "Did you mean <X>, or should I propose options?").

Do not re-ask the same question if you already got a usable direct answer. Empty or irrelevant replies → clarify or reask.

Examples — asking (closed-form):
{
  "question": "Choose one:\\nA) Refactor using Strategy pattern\\nB) Add flags to existing function\\nC) Create new module\\nReply A, B, or C."
}

Examples — asking (open, with context):
{
  "question": "I found conflicting requirements in docs/ARCHITECTURE.md and src/config.rs.\\nWhich behavior should take precedence: strict validation or backward compatibility?"
}

Example — after meta-question (investigate first, then one turn):
Previous ask: "Which architecture: A) centralized manager, B) direct, C) plugin?"
User: "what do you recommend?"
→ Read relevant code if needed, then:
{
  "question": "From the current wiring I recommend **A (centralized manager)** because it scales with more MCP servers and keeps one integration surface.\\n\\nDoes A work for you, or compare B/C in more detail?"
}

Example — after delegation (propose, then confirm):
User: "you decide"
→ Research/weigh, then:
{
  "question": "Given the tradeoffs above, I propose **B (direct wiring)** for this repo size: less indirection, easier debugging.\\n\\nConfirm B, or prefer A/C?"
}
Do not implement until they confirm.
"""
        return description

    async def acall(self, question: str, **kwargs) -> str:
        """Асинхронный вход. Вопрос виден ДО того, как ждём ответ.

        Порядок несущий. Раньше ответ читался первым, а проверки, печать
        вопроса, лог и событие прогресса шли в общем теле уже с готовым
        ответом. В CLI человек видел вопрос только после того, как вслепую
        ответил; баннер субагента в TUI (он рисуется по событию прогресса) не
        появлялся вовсе; пустой и слишком длинный вопрос сначала ждали ввода и
        лишь потом отклонялись.

        Ожидание человека не занимает поток пула: встраивающий отвечает через
        свой порт ``ctx.ask_human``, без порта — ``input()`` в потоке.
        """
        prepared = self._begin_ask(question)
        if isinstance(prepared, str):
            return prepared  # проверка не прошла — ждать нечего
        question, via_name = prepared
        try:
            response = await _aread_human_response(self._ctx, question)
        except (KeyboardInterrupt, HumanRequestInterrupted):
            return self._ask_interrupted(question, via_name)
        except Exception as exc:  # EOFError, отказ порта встраивающего
            return self._ask_failed(question, via_name, exc)
        except BaseException:
            # `CancelledError` — не `Exception`, и проходила мимо: событие
            # `ask_human` из `_begin_ask` оставалось незакрытым, а баннер
            # «агент спрашивает» висел в TUI и у встраивающего после отмены
            # `stream()` или остановки сервера. Закрываем и пробрасываем.
            from gc_core.tools.subagents.progress import emit_progress

            emit_progress(
                self._ctx,
                self._state,
                "ask_human_done",
                tool_name=via_name,
                ok=False,
            )
            raise
        if isinstance(response, HumanAnswer):
            self._queue_answer_images(response.attachments)
            response = _answer_text(response)
        return self._ask_answered(question, via_name, response)

    def _begin_ask(self, question: Any):
        """Проверки, нормализация, событие прогресса и печать вопроса.

        Возвращает конверт-отказ (строку) или пару ``(вопрос, имя субагента)``.
        """
        if not isinstance(question, str):
            return self._result(
                ok=False,
                question=str(question),
                response="",
                error={
                    "type": "invalid_argument",
                    "field": "question",
                    "message": f"Argument 'question' must be a string. Got: {type(question)} ({question!r})",
                },
            )
        if not question.strip():
            return self._result(
                ok=False,
                question=question,
                response="",
                error={
                    "type": "invalid_argument",
                    "field": "question",
                    "message": "Argument 'question' cannot be empty or whitespace only.",
                },
            )
        if len(question.strip()) > 4096:
            return self._result(
                ok=False,
                question=question,
                response="",
                error={
                    "type": "invalid_argument",
                    "field": "question",
                    "message": f"Question too long ({len(question)} characters). Keep under 4096.",
                },
            )

        question = self._normalize_text(question)

        from gc_core.tools.subagents.progress import emit_progress

        via_name = getattr(self._ctx, "subagent_tool_name", None)
        via_name = via_name if isinstance(via_name, str) else None
        emit_progress(
            self._ctx,
            self._state,
            "ask_human",
            prose=question,
            tool_name=via_name,
        )
        self._console("\n" + "=" * 60)
        self._console("AI ASKS HUMAN:")
        self._console("-" * 60)
        self._console(question)
        self._console("-" * 60)
        self._console(
            "Please enter your response (end with EOF or empty line):"
        )
        self._run_logger().info(
            "ask_human_question",
            extra={"event": "ask_human_question", "question": question},
        )
        return question, via_name

    def _ask_answered(
        self, question: str, via_name: Optional[str], response: str
    ) -> str:
        from gc_core.tools.subagents.progress import emit_progress

        if response == "":
            response = "(empty response)"
        self._console(f"Human responded: {response!r}")
        self._console("=" * 60 + "\n")
        self._run_logger().info(
            "ask_human_response",
            extra={"event": "ask_human_response", "response": response},
        )
        emit_progress(
            self._ctx,
            self._state,
            "ask_human_done",
            tool_name=via_name,
            ok=True,
        )
        return self._result(
            ok=True,
            question=question,
            response=response,
            error=None,
            hint=_RESPONSE_HINT,
        )

    def _queue_answer_images(
        self, attachments: tuple[Attachment, ...]
    ) -> None:
        """Картинки ответа — в тред и в хвост батча (``state.pending_tool_attachments``).

        Ядро — единственный владелец ``commit`` (спека §3): фронт отдаёт вложения
        из кармана как есть. Прогон без треда оставляет их в кармане процесса.
        Файл кармана пропал (ручное удаление) — ``commit`` сам возвращает ссылку
        как есть: проекция пошлёт на её месте «файл недоступен», текст ответа
        доходит (так же ведёт себя commit сообщения пользователя, часть F).
        """
        for att in attachments:
            self._state.pending_tool_attachments.append(
                commit_to_run_thread(self._ctx, att).to_dict()
            )

    def _console(self, *values: Any) -> None:
        """Строка диалога с человеком в stderr (CLI). В durable-исполнении
        (код workflow) — нет: вопрос идёт приложению, а печать повторялась
        бы при каждом replay; вопрос и ответ остаются в журнале прогона."""
        if not getattr(self._ctx, "durable_execution", False):
            print(*values, file=sys.stderr)

    def _ask_interrupted(self, question: str, via_name: Optional[str]) -> str:
        from gc_core.tools.subagents.progress import emit_progress

        self._console("\n\n[Interaction interrupted by user]")
        self._run_logger().warning(
            "ask_human_interrupted",
            extra={"event": "ask_human_interrupted"},
        )
        emit_progress(
            self._ctx,
            self._state,
            "ask_human_done",
            tool_name=via_name,
            ok=False,
            error_brief="interrupted",
        )
        return self._result(
            ok=False,
            question=question,
            response="(user interrupted)",
            error={
                "type": "interrupted",
                "message": "Interaction interrupted by user.",
            },
        )

    def _ask_failed(
        self, question: str, via_name: Optional[str], exc: Exception
    ) -> str:
        from gc_core.tools.subagents.progress import emit_progress

        error_msg = f"(error reading input: {exc!s})"
        self._console(f"[turn_ask_human error]: {error_msg}")
        self._run_logger().warning(
            "ask_human_error",
            extra={"event": "ask_human_error", "error": error_msg},
        )
        emit_progress(
            self._ctx,
            self._state,
            "ask_human_done",
            tool_name=via_name,
            ok=False,
            error_brief=str(exc),
        )
        return self._result(
            ok=False,
            question=question,
            response=error_msg,
            error={"type": "read_failed", "message": str(exc)},
        )

    def _normalize_text(self, text: str) -> str:
        """Normalize line endings and clean up extra whitespace."""
        # Convert all line breaks to \n
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Strip leading/trailing whitespace per line, but preserve internal structure
        lines = [line.rstrip() for line in text.split("\n")]
        # Remove trailing empty lines
        while lines and lines[-1] == "":
            lines.pop()
        while lines and lines[0] == "":
            lines.pop(0)
        return "\n".join(lines)
