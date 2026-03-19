from tools.simple_tools.write_to_file_tool import WriteToFile
from tools.simple_tools.append_to_file import AppendToFile
from tools.simple_tools.read_file import ReadFile
from tools.simple_tools.list_files import ListFiles
from tools.simple_tools.search_files import SearchFiles
from tools.simple_tools.ask_human import AskHuman
from tools.simple_tools.execute_command import ExecuteCommand
from tools.simple_tools.search_and_replace import SearchAndReplace
from tools.simple_tools.think import Think
from tools.simple_tools.thread_notes import ThreadNotes
from tools.simple_tools.submit_solution import SubmitSolution
from tools.simple_tools.calculator import Calculator
from tools.simple_tools.run_python_code import RunPythonCode
from tools.simple_tools.fetch_url import FetchUrl
from typing import Dict, Any


def get_simple_tools_data(config_dict: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Собрать словарь simple‑tools в формате tools_data.

    В зависимости от config_dict может быть добавлен инструмент ask_human.
    """
    simple_tools_data: Dict[str, Dict[str, Any]] = {
        'write_to_file': {
            'tool_fabric': WriteToFile,
            'spec': WriteToFile.tool_description(),
            'description': WriteToFile.prompt_description(),
        },
        'append_to_file': {
            'tool_fabric': AppendToFile,
            'spec': AppendToFile.tool_description(),
            'description': AppendToFile.prompt_description(),
        },
        'read_file': {
            'tool_fabric': ReadFile,
            'spec': ReadFile.tool_description(),
            'description': ReadFile.prompt_description(),
        },
        'list_files': {
            'tool_fabric': ListFiles,
            'spec': ListFiles.tool_description(),
            'description': ListFiles.prompt_description(),
        },
        'search_files': {
            'tool_fabric': SearchFiles,
            'spec': SearchFiles.tool_description(),
            'description': SearchFiles.prompt_description(),
        },
        'execute_command': {
            'tool_fabric': ExecuteCommand,
            'spec': ExecuteCommand.tool_description(),
            'description': ExecuteCommand.prompt_description(),
        },
        'search_and_replace': {
            'tool_fabric': SearchAndReplace,
            'spec': SearchAndReplace.tool_description(),
            'description': SearchAndReplace.prompt_description(),
        },
        'thread_notes': {
            'tool_fabric': ThreadNotes,
            'spec': ThreadNotes.tool_description(),
            'description': ThreadNotes.prompt_description(),
        },
        'think': {
            'tool_fabric': Think,
            'spec': Think.tool_description(),
            'description': Think.prompt_description(),
        },
        'calculator': {
            'tool_fabric': Calculator,
            'spec': Calculator.tool_description(),
            'description': Calculator.prompt_description(),
        },
        'run_python_code': {
            'tool_fabric': RunPythonCode,
            'spec': RunPythonCode.tool_description(),
            'description': RunPythonCode.prompt_description(),
        },
        'fetch_url': {
            'tool_fabric': FetchUrl,
            'spec': FetchUrl.tool_description(),
            'description': FetchUrl.prompt_description(),
        },
        'submit_solution': {
            'tool_fabric': SubmitSolution,
            'spec': SubmitSolution.tool_description(),
            'description': SubmitSolution.prompt_description(),
        },
    }

    if config_dict.get('use_ask_human_tool', False):
        simple_tools_data['ask_human'] = {
            'tool_fabric': AskHuman,
            'spec': AskHuman.tool_description(),
            'description': AskHuman.prompt_description(),
        }

    stop_tools_cfg = config_dict.get('stop_tools', [])
    if isinstance(stop_tools_cfg, list):
        stop_tools = {
            tool_name.strip()
            for tool_name in stop_tools_cfg
            if isinstance(tool_name, str) and tool_name.strip()
        }
        if stop_tools:
            simple_tools_data = {
                tool_name: tool_data
                for tool_name, tool_data in simple_tools_data.items()
                if tool_name not in stop_tools
            }

    return simple_tools_data


__all__ = ["get_simple_tools_data"]
