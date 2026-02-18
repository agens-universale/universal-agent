from abc import ABC, abstractmethod
from typing import Dict, Any

class AbstractSimpleTool(ABC):

    def __init__(self, *args, **kwargs):
        pass

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
