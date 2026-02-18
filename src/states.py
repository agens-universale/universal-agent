import os
import time
from uuid import uuid4
import json
from typing import List, Union, Dict, Optional
from utils.project_data import get_project_data
import config


class State:

    def __init__(self,
                 verbose: bool,
                 model: str,
                 tools_spec: List[Dict],
                 tools_descr: List[Dict],
                 max_steps: int,
                 parent_state_id: Optional[str] = None,
                 mode: str = None,
                 call_id: Optional[str] = None,
                 specific_role: Optional[str] = None
                 ):
        self.parent_state_id = parent_state_id
        self.state_id = str(uuid4())
        self.specific_role = specific_role
        self.verbose = verbose
        self.default_model = model
        self.initial_project_data = get_project_data()
        self.tools = tools_spec
        self.tools_descr = tools_descr
        self.call_metadata = self.set_call_metadata()
        self.messages = []
        self.previous_response_id = None
        self.mode = mode
        self.call_id = call_id
        self.max_steps = max_steps
        self.steps = 0

    def set_call_metadata(self):
        metadata = dict()
        metadata['systemPromptId'] = config.config_dict['system_prompt_id']
        metadata['promptVariables'] = self.initial_project_data
        if self.specific_role is not None:
            metadata['specificRole'] = self.specific_role
        metadata['useServerSubagents'] = config.config_dict.get('use_server_subagents', False)
        return metadata

    def to_dict(self):
        return vars(self).copy()

    def add_message(self, message):
        self.messages.append(message)

    def add_messages(self, messages):
        self.messages += messages

    def set_previous_response_id(self, previous_response_id):
        self.previous_response_id =previous_response_id

    def check_messages_in_state(self, messages) -> bool:
        """
        check messages by message['id']
        if one message or more from messages is in state.messages - return True, otherwise False
        :param messages:
        :return:
        """
        state_messages_ids = set([m['id'] for m in self.messages])
        for m in messages:
            m_id = m['id']
            if m_id in state_messages_ids:
                return True
        return False


class SatesStorage:

    def __init__(self):
        self.states = dict()

    def add_state(self, state: State):
        state_id = state.state_id
        self.states[state_id] = state

    def remove_state(self, state: State):
        state_id = state.state_id
        del self.states[state_id]

    def get_state_by_id(self, state_id):
        return self.states.get(state_id, None)

    def to_dict(self):
        return {
            state_id: state.to_dict() if hasattr(state, 'to_dict') else state.__dict__
            for state_id, state in self.states.items()
        }

    def save_to_file(self, filepath, **kwargs):
        with open(filepath, 'w', encoding='utf-8') as fd:
            json.dump(self.to_dict(), fd, ensure_ascii=False, **kwargs)


# Global object

state_storage = SatesStorage()

#####################


def save_state(state: State):
    global state_storage
    state_storage.add_state(state)

def dump_storage(cur_dir, **kwargs):
    global state_storage
    fp = os.path.join(cur_dir, f'session_storage_{int(time.time())}.json')
    state_storage.save_to_file(fp, **kwargs)
    print(f'\n\nStorage dumped in file {fp}\n\n')
