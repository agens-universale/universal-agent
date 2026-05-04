from abc import ABC, abstractmethod
from typing import Dict, Any

class AbstractSimpleTool(ABC):

    def __init__(self, *args, **kwargs):
        self._ctx = kwargs.get("ctx")
        self._state = kwargs.get("state")
        self._config_dict = kwargs.get("config_dict")

    @classmethod
    @abstractmethod
    def tool_description(self) -> Dict[str, Any]:
        pass

    @classmethod
    @abstractmethod
    def prompt_description(self) -> str:
        pass

    @abstractmethod
    def __call__(self, *args, **kwargs) -> str:
        pass
