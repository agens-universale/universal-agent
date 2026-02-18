from tools.simple_tools.write_to_file_tool import WriteToFile
from tools.simple_tools.read_file import ReadFile
from tools.simple_tools.list_files import ListFiles
from tools.simple_tools.search_files import SearchFiles
from tools.simple_tools.ask_human import AskHuman
from tools.simple_tools.execute_command import ExecuteCommand
from tools.simple_tools.search_and_replace import SearchAndReplace
from tools.simple_tools.update_plan import UpdatePlan
from tools.simple_tools.reflect import Reflect
from tools.simple_tools.submit_solution import SubmitSolution
import config


simple_tools_data = {
    'write_to_file': {
        'tool_fabric': WriteToFile,
        'spec': WriteToFile.tool_description(),
        'description': WriteToFile.prompt_description()
    },
    'read_file': {
        'tool_fabric': ReadFile,
        'spec': ReadFile.tool_description(),
        'description': ReadFile.prompt_description()
    },
    'list_files': {
        'tool_fabric': ListFiles,
        'spec': ListFiles.tool_description(),
        'description': ListFiles.prompt_description()
    },
    'search_files': {
        'tool_fabric': SearchFiles,
        'spec': SearchFiles.tool_description(),
        'description': SearchFiles.prompt_description()
    },
    'execute_command': {
        'tool_fabric': ExecuteCommand,
        'spec': ExecuteCommand.tool_description(),
        'description': ExecuteCommand.prompt_description()
    },
    'search_and_replace': {
        'tool_fabric': SearchAndReplace,
        'spec': SearchAndReplace.tool_description(),
        'description': SearchAndReplace.prompt_description()
    },
    'update_plan': {
        'tool_fabric': UpdatePlan,
        'spec': UpdatePlan.tool_description(),
        'description': UpdatePlan.prompt_description()
    },
    'reflect': {
        'tool_fabric': Reflect,
        'spec': Reflect.tool_description(),
        'description': Reflect.prompt_description()
    },
    'submit_solution': {
        'tool_fabric': SubmitSolution,
        'spec': SubmitSolution.tool_description(),
        'description': SubmitSolution.prompt_description()
    }
}

if config.config_dict.get('use_ask_human_tool', False):
    simple_tools_data['ask_human'] = {
        'tool_fabric': AskHuman,
        'spec': AskHuman.tool_description(),
        'description': AskHuman.prompt_description()
    }

simple_tools_spec = [v['spec'] for _,v in simple_tools_data.items()]

simple_tools_description = [v['description'] for _,v in simple_tools_data.items()]


if __name__ == '__main__':
    print(simple_tools_spec)
