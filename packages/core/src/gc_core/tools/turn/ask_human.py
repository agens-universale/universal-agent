import inspect
import sys
import json
import logging
from typing import Optional, Dict, Any
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




async def _aread_human_response(ctx: "Any", question: str) -> str:
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
    port = getattr(ctx, "ask_human", None)
    if callable(port):
        result = port(question)
        if inspect.isawaitable(result):
            result = await result
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
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
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
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        # Legacy channel (tools_descr); not wired to API today — reserved for a future
        # tool_help-style meta-tool. Agent-facing spec: tool_description + result hint.
        description = '''Tool name: turn_ask_human

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
'''
        return description


    async def acall(self, question: str, **kwargs) -> str:
        """Асинхронный вход: ожидание человека не занимает поток из пула.

        Ждать человека можно сколько угодно, и синхронный путь держал на этом
        слот общего пула ``to_thread`` (14 воркеров на всех) без всякого
        предела. Здесь ответ читается ДО тела, а тело получает его готовым:
        так у двух входов остаётся ровно одна разница — способ чтения, — и
        печать, логи и обработка ошибок не раздваиваются.
        """
        # Читаем ВНУТРИ колбэка, а не заранее. Разница несущая: `_ask_with`
        # ловит `KeyboardInterrupt` и превращает его в конверт «прервано», а
        # чтение до тела выносило Ctrl+C наружу — прогон падал вместо ответа.
        # Ответ уже получен к моменту вызова колбэка (`await` выше по стеку
        # невозможен внутри синхронного тела), поэтому читаем его отложенно
        # через futures-обёртку: корутина ждётся здесь, а исключение всплывает
        # в колбэке, то есть внутри try.
        pending: dict = {}

        try:
            pending["answer"] = await _aread_human_response(self._ctx, question)
        except BaseException as exc:          # noqa: BLE001 — переносим ЛЮБОЙ
            # Любой, а не только KeyboardInterrupt: тело разбирает и EOFError,
            # и ошибки порта встраивающего, и каждый исход у него свой конверт.
            # Перехват здесь только ПЕРЕНОСИТ исключение внутрь try тела.
            pending["error"] = exc

        def _read() -> str:
            if "error" in pending:
                raise pending["error"]
            return pending["answer"]

        return self._ask_with(question, _read)

    def _ask_with(self, question: str, read_response: "Any") -> str:
        """Общее тело: проверки, печать, логи, прогресс и разбор ответа.

        ``read_response`` — нульарный колбэк, отдающий ответ человека. Он и
        есть единственное, чем отличаются синхронный и асинхронный входы.
        """
        # Input validation
        if not isinstance(question, str):
            return self._result(
                ok=False,
                question=str(question),
                response="",
                error={
                    "type": "invalid_argument",
                    "field": "question",
                    "message": f"Argument 'question' must be a string. Got: {type(question)} ({repr(question)})",
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

        # Sanitize line endings and normalize whitespace
        question = self._normalize_text(question)

        from gc_core.tools.subagents.progress import emit_progress

        via_name = getattr(self._ctx, "subagent_tool_name", None)
        emit_progress(
            self._ctx,
            self._state,
            "ask_human",
            prose=question,
            tool_name=via_name if isinstance(via_name, str) else None,
        )

        try:
            # Print the question to stdout (visible to user)
            print("\n" + "="*60, file=sys.stderr)
            print("AI ASKS HUMAN:", file=sys.stderr)
            print("-"*60, file=sys.stderr)
            print(question, file=sys.stderr)
            print("-"*60, file=sys.stderr)
            print("Please enter your response (end with EOF or empty line):", file=sys.stderr)
            self._run_logger().info(
                "ask_human_question",
                extra={"event": "ask_human_question", "question": question},
            )

            response = read_response()

            if response == "":
                response = "(empty response)"

            print(f"Human responded: {repr(response)}", file=sys.stderr)
            print("="*60 + "\n", file=sys.stderr)
            self._run_logger().info(
                "ask_human_response",
                extra={"event": "ask_human_response", "response": response},
            )

            emit_progress(
                self._ctx,
                self._state,
                "ask_human_done",
                tool_name=via_name if isinstance(via_name, str) else None,
                ok=True,
            )

            return self._result(
                ok=True,
                question=question,
                response=response,
                error=None,
                hint=_RESPONSE_HINT,
            )

        except KeyboardInterrupt:
            print("\n\n[Interaction interrupted by user]", file=sys.stderr)
            self._run_logger().warning(
                "ask_human_interrupted",
                extra={"event": "ask_human_interrupted"},
            )
            emit_progress(
                self._ctx,
                self._state,
                "ask_human_done",
                tool_name=via_name if isinstance(via_name, str) else None,
                ok=False,
                error_brief="interrupted",
            )
            return self._result(
                ok=False,
                question=question,
                response="(user interrupted)",
                error={"type": "interrupted", "message": "Interaction interrupted by user."},
            )
        except Exception as e:
            # Fallback in case of unexpected error
            error_msg = f"(error reading input: {str(e)})"
            print(f"[turn_ask_human error]: {error_msg}", file=sys.stderr)
            self._run_logger().warning(
                "ask_human_error",
                extra={"event": "ask_human_error", "error": error_msg},
            )
            emit_progress(
                self._ctx,
                self._state,
                "ask_human_done",
                tool_name=via_name if isinstance(via_name, str) else None,
                ok=False,
                error_brief=str(e),
            )
            return self._result(
                ok=False,
                question=question,
                response=error_msg,
                error={"type": "read_failed", "message": str(e)},
            )

    def _normalize_text(self, text: str) -> str:
        """Normalize line endings and clean up extra whitespace."""
        # Convert all line breaks to \n
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        # Strip leading/trailing whitespace per line, but preserve internal structure
        lines = [line.rstrip() for line in text.split('\n')]
        # Remove trailing empty lines
        while lines and lines[-1] == '':
            lines.pop()
        while lines and lines[0] == '':
            lines.pop(0)
        return '\n'.join(lines)
