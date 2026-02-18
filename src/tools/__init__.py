import json
import traceback
import tools.simple_tools
import sys
import logging
import config
from tools.connect_mcp import get_mcp_tools_data
from tools.simple_tools import simple_tools_spec
from utils.convertations import tool_responces_to_chat_completion

logger = logging.getLogger(__name__)


tools_data = dict()

for k, v in tools.simple_tools.simple_tools_data.items():
    tools_data[k] = v

mcp_tools_data = get_mcp_tools_data()
for k, v in mcp_tools_data.items():
    tools_data[k] = v


logger.debug(str(tools_data))

tools_spec = [v['spec'] for _,v in tools_data.items()]
tools_description = [v['description'] for _,v in tools_data.items()]


def apply_tool(tool_name: str, arguments: str, tools_data_=None, **kwargs):
    global tools_data
    if tools_data_ is None:
        tools_data_ = tools_data
    arguments = json.loads(arguments)
    if config.config_dict.get('use_server_subagents', False):
        if tool_name.startswith(config.config_dict['server_tool_prefix']):
            return f'{tool_name} call'
    try:
        tool_fabric = tools_data_[tool_name]['tool_fabric']
    except KeyError:
        return f'{tool_name} - there is not such tool on client'
        logger.error(f'apply_tool log:\nkwargs:\n{kwargs}\narguments:\n {arguments}\nNo tool:\n{tool_name}')

    tool = tool_fabric(**kwargs)
    # return 'tool executed successfully'
    # print(f'kwargs:\n{kwargs}\narguments:\n {arguments}\ntool_fabric:\n{tool_fabric}\ntool:\n{tool}',  file=sys.stderr)
    logger.debug(f'apply_tool log:\nkwargs:\n{kwargs}\narguments:\n {arguments}\ntool_fabric:\n{tool_fabric}\ntool:\n{tool}')
    try:
        res = tool.__call__(**arguments)
    except Exception as e:
        res = str(e)
        res += '\n' + f"Agent tool execution Error in cli agent tool execution: {traceback.format_exc()}"
    if isinstance(res, str):
        res_str = res
    elif isinstance(res, list):
        res_str = json.dumps(res, ensure_ascii=False)
    elif isinstance(res, dict):
        res_str = json.dumps(res, ensure_ascii=False)
    else:
        res_str = str(res)

    return res_str


def run_test():
    class TestTool:
        def __call__(self, x,y):
            return x+y+1

    tools_data = {'test_tool': {'tool_fabric': TestTool}}
    assert apply_tool('test_tool', '{"x": 1, "y": 2}', tools_data) == 4


if __name__ == '__main__':
    # run_test()
    print(tools_spec)

