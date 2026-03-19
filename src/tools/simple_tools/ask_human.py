import sys
from typing import Optional, Dict, Any
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class AskHuman(AbstractSimpleTool):
    """
    Tool that allows the AI agent to ask a question and receive input from a human user.
    This is essential for clarifying ambiguous requirements, confirming decisions, or obtaining subjective feedback.
    """

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "ask_human",
            "description": "Ask a question to the human user and wait for their response. Use this when clarification, confirmation, or subjective input is needed. The question must be clear, concise, and include necessary context. Avoid vague phrasing; prefer open-ended questions or multiple-choice options to minimize follow-up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The clear, concise question to ask the human. Should be self-contained and include relevant context if needed (e.g., code snippets, options). Keep under 100 words when possible."
                    }
                },
                "required": ["question"],
                "additionalProperties": False
            },
            "strict": True
        }

    @classmethod
    def prompt_description(cls) -> str:
        description = '''Tool name: ask_human  
Tool description: Asks a question to the human user and waits for their textual response. Essential for resolving ambiguity, confirming critical actions, or gathering subjective opinions.
Input schema: {"type": "object", "properties": {"question": {"type": "string", "description": "The clear, concise question to ask the human. Should be self-contained."}}, "required": ["question"], "additionalProperties": false}

Usage:
- Use this tool **only** when you cannot proceed confidently without human input.
- The human will read your question and type a free-form answer.
- Always phrase questions clearly and unambiguously.
- Avoid yes/no unless necessary; prefer open-ended or multiple-choice when context helps.
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
'''
        return description

    def __call__(self, question: str, **kwargs) -> Dict[str, str]:
        # Input validation
        if not isinstance(question, str):
            raise ValueError(f"Argument 'question' must be a string. Got: {type(question)} ({repr(question)})")
        if not question.strip():
            raise ValueError("Argument 'question' cannot be empty or whitespace only.")
        if len(question.strip()) > 4096:
            raise ValueError(f"Question too long ({len(question)} characters). Keep under 4096.")

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

            return {"response": response}

        except KeyboardInterrupt:
            print("\n\n[Interaction interrupted by user]", file=sys.stderr)
            return {"response": "(user interrupted)"}
        except Exception as e:
            # Fallback in case of unexpected error
            error_msg = f"(error reading input: {str(e)})"
            print(f"[ask_human error]: {error_msg}", file=sys.stderr)
            return {"response": error_msg}

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
