import json
import sys
from typing import Any, Dict, Optional

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool

_MAX_DIRECT_ANSWER_CHARS = 4096

_TOOL_DESCRIPTION = (
    "Provide a direct answer to the human user without requesting input. "
    "Use this for one-way explanations, clarifications, or status updates. "
    "Keep `answer` concise; for long material provide it as a separate user-facing artifact "
    "and include only a short summary plus artifact path in `answer`. "
    "Omit `artifact_path` when there is no separate artifact; if set, it must be a non-empty path."
)


class AnswerQuestion(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        answer: str,
        artifact_path: str | None = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        out: Dict[str, Any] = {
            "tool": "answer_question",
            "ok": ok,
            "answer": answer,
            "error": error,
        }
        if artifact_path is not None:
            out["artifact_path"] = artifact_path
        return json.dumps(out, ensure_ascii=False)

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "answer_question",
            "description": _TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "Concise user-facing answer text. Avoid long narratives; "
                            "if details are long, provide short summary here and put full text into an artifact."
                        ),
                    },
                    "artifact_path": {
                        "type": "string",
                        "description": (
                            "Optional. Path to a detailed user-facing artifact. "
                            "Omit this field entirely when there is no artifact — do not pass an empty string."
                        ),
                    },
                },
                "required": ["answer"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return """Tool name: answer_question

Overview:
Deliver a direct answer to the user (one-way output). This tool does not ask for a reply.

Input schema:
{"type":"object","properties":{"answer":{"type":"string"},"artifact_path":{"type":"string"}},"required":["answer"],"additionalProperties":false}

Usage:
- Use for direct explanations, clarifications, and updates.
- Keep `answer` concise and readable.
- For long content, create a user-facing artifact and include only a brief summary in `answer` plus `artifact_path` (omit the field when unused).
- Hard limit: 4096 characters in `answer`.
"""

    def __call__(self, answer: str, artifact_path: str | None = None, **kwargs) -> str:
        if not isinstance(answer, str):
            return self._result(
                ok=False,
                answer=str(answer),
                artifact_path=artifact_path if isinstance(artifact_path, str) else None,
                error={
                    "type": "invalid_argument",
                    "field": "answer",
                    "message": f"Argument 'answer' must be a string. Got: {type(answer)} ({repr(answer)})",
                },
            )

        answer = answer.strip()
        if not answer:
            return self._result(
                ok=False,
                answer="",
                artifact_path=artifact_path if isinstance(artifact_path, str) else None,
                error={
                    "type": "invalid_argument",
                    "field": "answer",
                    "message": "Argument 'answer' cannot be empty or whitespace only.",
                },
            )

        if len(answer) > _MAX_DIRECT_ANSWER_CHARS:
            return self._result(
                ok=False,
                answer=answer,
                artifact_path=artifact_path if isinstance(artifact_path, str) else None,
                error={
                    "type": "invalid_argument",
                    "field": "answer",
                    "message": (
                        f"Answer too long ({len(answer)} characters). "
                        f"Keep under {_MAX_DIRECT_ANSWER_CHARS} and move details to an artifact."
                    ),
                },
            )

        normalized_artifact: str | None = None
        if artifact_path is not None:
            if not isinstance(artifact_path, str):
                return self._result(
                    ok=False,
                    answer=answer,
                    error={
                        "type": "invalid_argument",
                        "field": "artifact_path",
                        "message": (
                            f"Argument 'artifact_path' must be a string when provided. "
                            f"Got: {type(artifact_path)} ({repr(artifact_path)})"
                        ),
                    },
                )
            stripped = artifact_path.strip()
            if stripped:
                normalized_artifact = stripped
            # Empty/whitespace artifact_path → treat as omitted (models often send "").

        print("\n" + "=" * 60, file=sys.stderr)
        print("AI ANSWERS HUMAN:", file=sys.stderr)
        print("-" * 60, file=sys.stderr)
        print(answer, file=sys.stderr)
        if normalized_artifact:
            print(f"\nDetailed artifact: {normalized_artifact}", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)

        return self._result(
            ok=True,
            answer=answer,
            artifact_path=normalized_artifact,
            error=None,
        )

