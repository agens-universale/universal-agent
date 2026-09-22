"""Load / save multi-state JSON dumps (session storage)."""

from __future__ import annotations

import json
from typing import Any, Dict

from gc_core.persistence.state_serializer import StateSerializer
from gc_core.states import State


class StateRepository:
    @staticmethod
    def serialize_storage(states: Dict[str, State]) -> Dict[str, Any]:
        return {state_id: StateSerializer.serialize(state) for state_id, state in states.items()}

    @staticmethod
    def deserialize_storage(data: Dict[str, Any]) -> Dict[str, State]:
        if not isinstance(data, dict):
            raise TypeError("StateRepository.deserialize_storage expects dict")
        restored: Dict[str, State] = {}
        for state_id, payload in data.items():
            if not isinstance(payload, dict):
                continue
            state = StateSerializer.deserialize(payload)
            if isinstance(state_id, str) and state_id.strip():
                state.state_id = state_id
            restored[state.state_id] = state
        return restored

    @staticmethod
    def save_snapshot(states: Dict[str, State], filepath: str, **json_kwargs: Any) -> None:
        payload = StateRepository.serialize_storage(states)
        with open(filepath, "w", encoding="utf-8") as fd:
            json.dump(payload, fd, ensure_ascii=False, **json_kwargs)

    @staticmethod
    def load_snapshot(filepath: str) -> Dict[str, State]:
        with open(filepath, encoding="utf-8") as fd:
            data = json.load(fd)
        return StateRepository.deserialize_storage(data)
