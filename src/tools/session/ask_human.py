import sys
import json
from typing import Optional, Dict, Any
from tools.simple_tool_base import AbstractSimpleTool

# Shown on every successful ask_human observation (agent-visible JSON).
_RESPONSE_HINT = (
    "Classify the reply before advancing. "
    "Direct answer (specific choice or instruction) → use it and proceed. "
    "Delegation (user leaves the call to you: 'you decide', 'your call', "
    "'как считаешь', 'на твоё усмотрение') → do not decide silently. "
    "Investigate/weigh with other tools, then ask_human with your proposed choice, "
    "reasoning, and explicit confirmation; proceed only after they agree — not a black box. "
    "Meta-question (user asks you back, e.g. 'what do you recommend?') → "
    "do not skip to the next task step. If you need facts, investigate with other tools "
    "first; reflect, then ask_human in one turn with your answer, reasoning, and "
    "ask whether they agree or want to settle the original choice — not a snap reply. "
    "Ambiguous, empty, or off-topic → ask_human again to clarify. "
    "If still insufficient and the user did not delegate, keep asking — do not guess."
)

_TOOL_DESCRIPTION = (
    "Ask the human user and wait for their free-form reply. "
    "Use for clarification, confirmation, subjective judgment, or continuing a dialogue. "
    "Be clear and include necessary context; avoid vague questions. "
    "The `question` must be concise and should contain only the actual question (plus minimal context). "
    "Do not put long explanations, full comparisons, or large background dumps into `question`; "
    "deliver detailed material as a separate user-facing artifact when you can, and keep `question` to a short prompt that points to that artifact and states what you need. "
    "When a finite choice set fits, prefer explicit options (A/B/C, numbered list, yes/no) "
    "to minimize follow-up; otherwise ask openly. "
    "After each call, follow the hint field in the tool result to handle the user's reply."
)


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
            "tool": "ask_human",
            "ok": ok,
            "error": error,
            "question": question,
            "response": response,
        }
        if hint:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False)

    """
    Tool that allows the AI agent to ask a question and receive input from a human user.
    This is essential for clarifying ambiguous requirements, confirming decisions, or obtaining subjective feedback.
    """

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "ask_human",
            "description": _TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "Concise message to the human (markdown ok): the question, minimal context, "
                            "and explicit answer options when choices are known. Not for long content—"
                            "provide that as a separate artifact and reference it here."
                        ),
                    }
                },
                "required": ["question"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls) -> str:
        # Legacy channel (tools_descr); not wired to API today — reserved for a future
        # tool_help-style meta-tool. Agent-facing spec: tool_description + result hint.
        description = '''Tool name: ask_human

Overview:
Ask the human user and wait for a free-form textual reply. Use for clarification,
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
   **Not** permission to act silently. Investigate/weigh with other tools, then `ask_human` with your **proposed** choice, tradeoffs, and explicit confirmation. Proceed only after they agree — not a black box.

3. **Meta-question or follow-up** — user asks YOU back ("what do you recommend?", "explain X", trailing "?").
   Do **not** skip to the next methodology step. If facts are needed, use other tools first; reflect; then one `ask_human` turn with your answer, reasoning, and ask to close the original choice — not a snap reply. Pacing follows the user.

4. **Ambiguous / empty / off-topic** — `ask_human` again to clarify (e.g. "Did you mean <X>, or should I propose options?").

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

    def __call__(self, question: str, **kwargs) -> str:
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

        try:
            # Print the question to stdout (visible to user)
            print("\n" + "="*60, file=sys.stderr)
            print("AI ASKS HUMAN:", file=sys.stderr)
            print("-"*60, file=sys.stderr)
            print(question, file=sys.stderr)
            print("-"*60, file=sys.stderr)
            print("Please enter your response (end with EOF or empty line):", file=sys.stderr)

            # Read multi-line input until EOF or empty line
            lines = []
            while True:
                try:
                    line = input()
                    if line == "":
                        break
                    lines.append(line)
                except EOFError:
                    break

            response = "\n".join(lines).strip()

            if response == "":
                response = "(empty response)"

            print(f"Human responded: {repr(response)}", file=sys.stderr)
            print("="*60 + "\n", file=sys.stderr)

            return self._result(
                ok=True,
                question=question,
                response=response,
                error=None,
                hint=_RESPONSE_HINT,
            )

        except KeyboardInterrupt:
            print("\n\n[Interaction interrupted by user]", file=sys.stderr)
            return self._result(
                ok=False,
                question=question,
                response="(user interrupted)",
                error={"type": "interrupted", "message": "Interaction interrupted by user."},
            )
        except Exception as e:
            # Fallback in case of unexpected error
            error_msg = f"(error reading input: {str(e)})"
            print(f"[ask_human error]: {error_msg}", file=sys.stderr)
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
