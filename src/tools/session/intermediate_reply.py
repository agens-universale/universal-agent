import json
import sys
from typing import Any, Dict, Optional

from tools.simple_tool_base import AbstractSimpleTool

_MAX_REPLY_CHARS = 4096

_TOOL_DESCRIPTION = (
    "Finalizes turn: no. "
    "Intermediate user-visible reply: status, hypothesis, mini-explanation, or progress update. "
    "The current task is NOT complete — you will continue with more tools after this. "
    "Do NOT use for the final report of a multi-step task or to end the current user turn. "
    "Do NOT use when you need the user's answer — this tool does not wait for a reply. "
    "Keep `answer` concise; for long material use a separate artifact and put only summary + path in `answer`. "
    "Omit `artifact_path` when there is no artifact; if set, it must be a non-empty path."
)


class IntermediateReply(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        answer: str,
        artifact_path: str | None = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        out: Dict[str, Any] = {
            "tool": "intermediate_reply",
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
            "name": "intermediate_reply",
            "description": _TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": (
                            "Concise intermediate user-facing text (status, partial insight, short clarification). "
                            "Not a final task report. For long content: short summary here; full text in artifact."
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
        return """Tool name: intermediate_reply

Overview:
Finalizes turn: no. Send a one-way message to the user while the turn is still in progress (status, hypothesis, mini-explanation, progress). The turn is NOT complete after this call.

Input schema:
{"type":"object","properties":{"answer":{"type":"string"},"artifact_path":{"type":"string"}},"required":["answer"],"additionalProperties":false}

Usage:
- Use only while more tool work remains in this turn.
- Do NOT use for the final turn report or to end the current user turn.
- Do NOT use when you need the user's input — this tool does not wait for a reply.
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

        if len(answer) > _MAX_REPLY_CHARS:
            return self._result(
                ok=False,
                answer=answer,
                artifact_path=artifact_path if isinstance(artifact_path, str) else None,
                error={
                    "type": "invalid_argument",
                    "field": "answer",
                    "message": (
                        f"Answer too long ({len(answer)} characters). "
                        f"Keep under {_MAX_REPLY_CHARS} and move details to an artifact."
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

        print("\n" + "=" * 60, file=sys.stderr)
        print("INTERMEDIATE REPLY:", file=sys.stderr)
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
