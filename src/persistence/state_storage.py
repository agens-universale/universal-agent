"""In-process State registry with v2 snapshot dump/load."""

from __future__ import annotations

import os
import time
from typing import Dict

from persistence.state_repository import StateRepository
from states import State


class StateStorage:
    def __init__(self):
        self.states: Dict[str, State] = {}

    def add_state(self, state: State) -> None:
        self.states[state.state_id] = state

    def remove_state(self, state: State) -> None:
        del self.states[state.state_id]

    def get_state_by_id(self, state_id: str) -> State | None:
        return self.states.get(state_id)

    def save_to_file(self, filepath: str, **kwargs) -> None:
        StateRepository.save_snapshot(self.states, filepath, **kwargs)

    def load_from_file(self, filepath: str) -> None:
        self.states = StateRepository.load_snapshot(filepath)


state_storage = StateStorage()


def save_state(state: State) -> None:
    state_storage.add_state(state)


def dump_storage(cur_dir: str, *, logger=None, console: bool = True, **kwargs) -> None:
    fp = os.path.join(cur_dir, f"session_storage_{int(time.time())}.json")
    state_storage.save_to_file(fp, **kwargs)
    if console:
        print(f"\n\nStorage dumped in file {fp}\n\n")
    if logger is not None:
        logger.info("storage_dumped", extra={"event": "storage_dumped", "path": fp})
