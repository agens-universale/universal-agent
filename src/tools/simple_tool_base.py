from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class AbstractSimpleTool(ABC):
    def __init__(self, *args, **kwargs):
        self._ctx = kwargs.get("ctx")
        self._state = kwargs.get("state")
        self._config_dict = kwargs.get("config_dict")

    @classmethod
    @abstractmethod
    def tool_description(cls) -> Dict[str, Any]:
        pass

    @classmethod
    @abstractmethod
    def prompt_description(cls) -> str:
        pass

    @abstractmethod
    def __call__(self, *args, **kwargs) -> str:
        pass

    @classmethod
    def wrong_arguments_feedback_hint(
        cls,
        *,
        received_args: str,
        parsed_args: dict[str, Any] | None,
        _message: str,
    ) -> str | None:
        """
        Optional extra LLM hint when argument validation fails for this tool.

        Return None if there is nothing tool-specific to add.
        """
        return None

    @classmethod
    def tool_output_on_cycle_stop(cls) -> Optional[str]:
        """
        Optional synthetic tool output for cycle-stop tools in chat/completions mode.
        Return None for regular tools.
        """
        return None

    @staticmethod
    def fallback_tool_output_on_cycle_stop(tool_name: str) -> str:
        payload = {
            "tool": str(tool_name or "unknown_tool"),
            "ok": False,
            "status": "skipped_on_cycle_stop",
            "message": "Tool call was not executed due to cycle stop or interruption.",
            "error": None,
            "synthetic": True,
        }
        import json
        return json.dumps(payload, ensure_ascii=False)
