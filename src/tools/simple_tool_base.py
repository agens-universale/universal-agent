import json
from abc import ABC, abstractmethod
from types import SimpleNamespace
from typing import Any, Dict, Optional

from tools.backends.local import LocalFsBackend


class AbstractSimpleTool(ABC):
    def __init__(self, *args, **kwargs):
        self._ctx = kwargs.get("ctx")
        self._state = kwargs.get("state")
        self._config_dict = kwargs.get("config_dict")
        if self._ctx is None:
            cfg = self._config_dict if isinstance(self._config_dict, dict) else {}
            self._ctx = SimpleNamespace(config_dict=cfg, os_backend=LocalFsBackend())

    @property
    def _backend(self):
        """Physical I/O port from ``RunContext.os_backend`` (set in ``create_run_context``)."""
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            raise RuntimeError(
                f"{type(self).__name__} requires ctx; use apply_tool or bind_os_backend in tests"
            )
        backend = getattr(ctx, "os_backend", None)
        if backend is None:
            raise RuntimeError(
                f"{type(self).__name__}: ctx.os_backend is missing; use create_run_context()"
            )
        return backend

    def bind_os_backend(self, backend) -> None:
        """Test hook: attach a backend without building a full RunContext."""
        ctx = getattr(self, "_ctx", None)
        if ctx is None:
            from types import SimpleNamespace

            ctx = SimpleNamespace(config_dict={})
            self._ctx = ctx
        ctx.os_backend = backend

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
    def prepare_args_before_strip(cls, args: Dict[str, Any]) -> Dict[str, Any]:
        """Adjust parsed args before injected schema fields are stripped.

        Override in tools that need to preserve injected values (e.g. remap
        injected ``reason`` into a native field). Default: no-op.
        """
        return args

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
        return json.dumps(payload, ensure_ascii=False)
