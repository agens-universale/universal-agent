import os
import sys
import config
import arg_parser
from processing_responses import run_agent
from states import State, save_state, dump_storage
import tools
from tools.skills import get_skills_registry
import utils.logging_module
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func

import logging

logger = logging.getLogger(__name__)

cur_dir = os.getcwd()


def main():
    global cur_dir

    verbose = arg_parser.run_args.verbose if arg_parser.run_args.verbose else config.config_dict.get('verbose', False)

    skills_list = get_skills_registry().list_skills()

    state_data = {
        'parent_state_id': None,
        'verbose': verbose,
        'model': config.config_dict['model'],
        'tools_spec': tools.tools_spec,
        'tools_descr': tools.tools_description,
        'max_steps': config.config_dict['max_steps'],
        'skills_list': skills_list
    }


    state = State(**state_data)
    save_state(state)
    message = {'role': 'user', 'content': arg_parser.run_args.query}
    # print(config.config_dict)
    # print(state.to_dict())
    # print(message)

    messages = []
    messages.append(message)
    for m in messages:
        m['id'] = generate_message_id_func('m')

    # print(state.to_dict())
    # print(messages)
    # sys.exit(0)

    response = run_agent(messages, state)

    if config.config_dict['dump_storage']:
        dump_storage(cur_dir, indent=2)

    print(f'\nПуть к файлу с логами: {utils.logging_module.logging_path}\n')

    return response


if __name__ == '__main__':
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
