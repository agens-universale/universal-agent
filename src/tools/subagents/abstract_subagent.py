from abc import ABC, abstractmethod
from typing import Tuple, Dict


class AbstractSubagent(ABC):

    @classmethod
    @abstractmethod
    async def create_instance(cls, **kwargs) -> "AbstractSubagent":
        """
        Асинхронная фабрика для создания экземпляра.
        Должна быть реализована в подклассах.
        """
        ...

    @abstractmethod
    def get_initial_data(self) -> Tuple[str, str]:
        """
        Returns: Tuple[agent_role, initial_user_message]
        """
        pass

    @abstractmethod
    def get_agent_name(self) -> str: ...

    @classmethod
    @abstractmethod
    def get_agent_specification(cls) -> Dict: ...
