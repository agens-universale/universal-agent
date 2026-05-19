import sys
import json
from typing import Optional, Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool

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
    "Send a message to the human user and wait for their free-form reply. "
    "Use whenever you need human input to continue: clarification, confirmation, "
    "subjective judgment, design discussion, or continuing a dialogue. "
    "The question field may be a short prompt or a longer turn — context, "
    "reasoning, partial findings, then what you need from the user. "
    "When a finite choice set fits, prefer a closed-form question with explicit "
    "options (A/B/C, numbered list, yes/no); use an open question when options "
    "would be incomplete, misleading, or worse than free-form. "
    "After each reply, classify it: (1) direct answer — proceed; "
    "(2) delegation — user defers to you: research, then ask_human with a declared "
    "proposal and confirmation before acting; "
    "(3) meta-question or follow-up — investigate/reflect if needed, then ask_human "
    "with your answer and ask for confirmation on the open decision; "
    "do not advance the methodology; "
    "(4) ambiguous/empty — ask_human again to clarify. "
    "If still insufficient and they did not delegate, keep asking; "
    "pacing follows the user, not your plan."
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
                            "What to show the human: question, request, or full conversational "
                            "turn (markdown ok). Prefer listed answer options when the decision "
                            "is essentially among known choices; otherwise ask openly."
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
Tool description: Asks a question to the human user and waits for their textual response. Essential for resolving ambiguity, confirming critical actions, or gathering subjective opinions.
Input schema: {"type": "object", "properties": {"question": {"type": "string", "description": "The clear, concise question to ask the human. Should be self-contained."}}, "required": ["question"], "additionalProperties": false}

Usage:
- Use this tool **only** when you cannot proceed confidently without human input.
- The human will read your question and type a free-form answer.
- Always phrase questions clearly and unambiguously.
- When a finite set of answers fits, prefer closed-form with explicit options (A/B/C, numbered, yes/no); use open-ended only when options would be incomplete or misleading.
- Include relevant context in the question (e.g., code snippets, options). In this case format your question with markdown.
- Do **not** use this tool for routine operations, searching, or debugging — use appropriate tools instead.

Important Rules:
- **NEVER** ask vague or overly broad questions like "What should I do?".
- Make your questions as short as possible. You have limit for length of question about 100 words, but less is better.
- If necessary provide **context**: reference files, code, or prior steps.
- Phrase questions so they can be answered with minimal back-and-forth.
- Avoid asking for trivial information available via other tools.
- Never ask the same question twice if you got answer earlier.
- Answer from user may be empty (empty response) or some irrelevant information. In such case you can reask.
- Escape special characters properly (use \\n for newlines, \\t for tabs).

Handling user's response — CRITICAL for natural conversation:

After receiving the user's response, ANALYZE it before advancing your reasoning. The response falls into one of four categories:

1. **Direct answer** — the user picked one of your options or gave a substantive answer to your question (e.g., "A", "yes", "option B sounds good", "use approach X"). Use it and proceed.

2. **Delegation to you** — the user leaves the decision to the agent (e.g., "you decide", "your call", "как считаешь", "на твоё усмотрение"). This is **not** permission to act silently. Investigate and weigh options (other tools as needed), then call `ask_human` with your **proposed** choice, tradeoffs, and ask for explicit confirmation. Proceed only after they agree — stay visible, not a black box.

3. **Meta-question or follow-up** — the user is asking YOU something instead of answering, or asking for elaboration. Common signals:
   • "What do you recommend?"
   • "Explain how X works" / "Tell me more about Y"
   • "What are the tradeoffs?"
   • "Why is this the case?"
   • Any question mark at the end of their reply.

   **DO NOT** advance to the next step of your task or methodology. Instead:
   • If their question needs evidence (code, docs, state), use other tools first — do not guess in ask_human.
   • Reflect, then re-call `ask_human` in **one** turn: your answer, brief reasoning, and ask whether they agree or want to close the original choice (refined framing ok). Same dialogue thread; not a one-line snap reply.
   • Pacing follows the user, not your internal task plan.

4. **Ambiguous** — unclear whether it's an answer, delegation, or a follow-up. Re-call `ask_human` with a clarifying yes/no: "Did you mean to choose <X>, or should I decide? (yes/no, or ask for more)".

This applies especially during multi-turn dialogues — brainstorming, design discussions, planning, debugging conversations — where back-and-forth is expected. Don't force-march through a methodology when the user wants to pause, drill in, or course-correct.

Examples:
{
  "question": "I found two conflicting requirements in docs/ARCHITECTURE.md and src/config.rs.\\nWhich behavior should take precedence: strict validation or backward compatibility?"
}

{
  "question": "Should I delete the deprecated function `legacy_auth()`? It's unused but referenced in comments.\\nReply 'yes' to remove or 'no' to keep."
}

{
  "question": "Choose one:\\nA) Refactor using Strategy pattern\\nB) Add flags to existing function\\nC) Create new module\\nEnter A, B, or C."
}

Re-ask after a meta-question — pattern:
Previous ask: "Which architecture: A) centralized manager, B) direct, C) plugin? (A/B/C)"
User responded: "what do you recommend?"
→ First: read relevant code/config if needed (other tools), then ask_human, e.g.:
{
  "question": "I looked at how MCP servers are wired today. I recommend **A (centralized manager)** because:\\n- Scales as more MCP servers are added.\\n- Single integration surface.\\n\\nDoes A work for you, or should we compare B/C in more detail?"
}
Not: a one-line opinion without looking, and not advancing to implementation before they confirm.
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
