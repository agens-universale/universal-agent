import json
import time
from typing import Any, Dict, Optional

from gc_core.tools.simple_tool_base import AbstractSimpleTool


class Sleep(AbstractSimpleTool):
    max_seconds = 180

    @staticmethod
    def _result(
        *,
        ok: bool,
        seconds: float | None = None,
        slept_seconds: float | None = None,
        error: Dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": Sleep.registered_tool_name(),
                "ok": ok,
                "seconds": seconds,
                "slept_seconds": slept_seconds,
                "error": error,
            },
            ensure_ascii=False,
        )

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return {
            "type": "function",
            "name": "exec_sleep",
            "description": (
                "Pauses execution for the given number of seconds before continuing. "
                f"Maximum sleep duration is {cls.max_seconds} seconds (3 minutes)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "description": "How long to sleep, in seconds (0 to 180).",
                    }
                },
                "required": ["seconds"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        return f'''Tool name: exec_sleep
Tool description: Pauses execution for the given number of seconds. Use when waiting for external processes, rate limits, or delayed availability.
Input schema: {{"type":"object","properties":{{"seconds":{{"type":"number","description":"Sleep duration in seconds (0 to {cls.max_seconds})."}}}},"required":["seconds"],"additionalProperties":false}}
Maximum sleep duration: {cls.max_seconds} seconds (3 minutes).

Return format (JSON string):
{{"tool":"exec_sleep","ok":true|false,"seconds":<requested>|null,"slept_seconds":<actual>|null,"error":null|{{"type":"...","message":"..."}}}}
'''

    def __call__(self, seconds: float | int | None = None, **kwargs) -> str:
        if seconds is None:
            return self._result(
                ok=False,
                seconds=None,
                slept_seconds=None,
                error={"type": "invalid_argument", "field": "seconds", "message": "seconds is required"},
            )

        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return self._result(
                ok=False,
                seconds=None,
                slept_seconds=None,
                error={"type": "invalid_argument", "field": "seconds"},
            )

        if seconds < 0:
            return self._result(
                ok=False,
                seconds=seconds,
                slept_seconds=None,
                error={"type": "invalid_argument", "message": "seconds must be >= 0"},
            )

        if seconds > self.max_seconds:
            return self._result(
                ok=False,
                seconds=seconds,
                slept_seconds=None,
                error={
                    "type": "duration_too_long",
                    "message": f"seconds must be <= {self.max_seconds}",
                    "max_seconds": self.max_seconds,
                },
            )

        time.sleep(seconds)
        return self._result(ok=True, seconds=seconds, slept_seconds=seconds, error=None)
