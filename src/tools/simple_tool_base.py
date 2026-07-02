from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class AbstractSimpleTool(ABC):
    def __init__(self, *args, **kwargs):
        self._ctx = kwargs.get("ctx")
        self._state = kwargs.get("state")
        self._config_dict = kwargs.get("config_dict")

    @property
    def _backend(self):
        """Physical I/O port: test override → ctx.harbor_backend → LocalFsBackend.

        Property (not __init__) so instances built via __new__ in tests resolve too.
        """
        override = self.__dict__.get("_backend_override")
        if override is not None:
            return override
        ctx = getattr(self, "_ctx", None)
        ctx_backend = getattr(ctx, "harbor_backend", None) if ctx is not None else None
        if ctx_backend is not None:
            return ctx_backend
        from tools.backends.local import LocalFsBackend
        return LocalFsBackend()

    @_backend.setter
    def _backend(self, value):
        self.__dict__["_backend_override"] = value

    @classmethod
    @abstractmethod
    def tool_description(cls) -> Dict[str, Any]:
        pass

    @classmethod
    def registered_tool_name(cls) -> str:
        """API tool name from ``tool_description()`` (same as ``tools_data`` key)."""
        spec = cls.tool_description()
        if isinstance(spec, dict):
            name = spec.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        return "unknown_tool"

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
