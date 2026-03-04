import json
import traceback
import sys
import logging
import config
from tools.connect_mcp import get_mcp_tools_data
from tools.skills import get_skills_tools_data
import tools.simple_tools as simple_tools
from utils.convertations import tool_responces_to_chat_completion

logger = logging.getLogger(__name__)


class WrongFunctionArgumentsException(Exception):
    def __init__(self, message: str, received_args: str, expected_schema: dict | None = None):
        super().__init__(message)
        self.message = message
        self.received_args = received_args
        self.expected_schema = expected_schema

    def to_llm_feedback(self) -> str:
        """
        Формирует четкое и структурированное сообщение об ошибке для LLM.
        """
        feedback = "❌ **Tool Argument Validation Failed**\n\n"
        
        # 1. Описание проблемы
        feedback += f"**Error:** {self.message}\n\n"
        
        # 2. Что было получено (с обработкой пустых значений)
        args_display = self.received_args if self.received_args else "(empty or missing)"
        feedback += f"**Received Arguments:**\n```json\n{args_display}\n```\n\n"
        
        # 3. Что ожидалось (если есть схема)
        if self.expected_schema:
            feedback += f"**Expected Schema:**\n```json\n{self.expected_schema}\n```\n\n"
        
        # 4. Четкая инструкция к действию
        feedback += (
            "**Action Required:**\n"
            "1. Review the error message and schema above.\n"
            "2. Correct the JSON arguments to match the expected schema.\n"
            "3. Ensure all required fields are present and have the correct types.\n"
            "4. Retry the tool call with the fixed arguments."
        )
        
        return feedback


tools_data = dict()

for k, v in simple_tools.simple_tools_data.items():
    tools_data[k] = v

skills_tools_data = get_skills_tools_data()
for k, v in skills_tools_data.items():
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

    if config.config_dict.get('use_server_subagents', False):
        if tool_name.startswith(config.config_dict['server_tool_prefix']):
            return f'{tool_name} call'

    try:
        tool_fabric = tools_data_[tool_name]['tool_fabric']
    except KeyError:
        return f'{tool_name} - there is not such tool on client'
        logger.error(f'apply_tool log:\nkwargs:\n{kwargs}\narguments:\n {arguments}\nNo tool:\n{tool_name}')

    # Определяем, ожидает ли инструмент пустую схему (нет параметров)
    spec = tools_data_[tool_name].get('spec', {}) or {}
    params_schema = spec.get('parameters', {}) or {}
    properties = params_schema.get('properties', {}) or {}
    required = params_schema.get('required', []) or []
    is_empty_schema = not properties and not required

    # Нормализуем и парсим аргументы
    if isinstance(arguments, dict):
        parsed_arguments = arguments
    else:
        arg_str = "" if arguments is None else str(arguments).strip()

        if arg_str == "":
            # Пустые аргументы:
            # - если схема пустая, подменяем на {} и вызываем инструмент без параметров;
            # - если схема не пустая, считаем это ошибкой и даём LLM подробный фидбек.
            if is_empty_schema:
                parsed_arguments = {}
            else:
                raise WrongFunctionArgumentsException(
                    message='аргументы инструмента пустые, ожидается валидный JSON-объект',
                    received_args=str(arguments),
                    expected_schema=tools_data_[tool_name]['spec']
                )
        else:
            try:
                parsed_arguments = json.loads(arg_str)
            except Exception as e:
                raise WrongFunctionArgumentsException(
                    message=f'некорректный JSON: {e}',
                    received_args=str(arguments),
                    expected_schema=tools_data_[tool_name]['spec']
                )

    arguments = parsed_arguments

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

