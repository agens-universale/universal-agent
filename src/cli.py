import os
from typing import Any

import arg_parser
from config import load_config, build_auth_connector
from processing_responses import run_agent
from run_context import RunContext, create_run_context
from states import State, save_state, dump_storage
from tools.skills.registry import get_skills_registry
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


def run(message: str, ctx: RunContext, *, verbose: bool | None = None) -> Any:
    """
    Основная функция одного запуска агента.
    Может вызываться как из CLI, так и из REST‑обёрток.
    """
    config_dict = ctx.config_dict

    effective_verbose = (
        verbose if verbose is not None else config_dict.get("verbose", False)
    )

    # Список доступных скиллов для включения в promptVariables
    skills_list = get_skills_registry(config_dict).list_skills()

    state_data = {
        "parent_state_id": None,
        "verbose": effective_verbose,
        "model": config_dict["model"],
        "tools_spec": ctx.tools_spec,
        "tools_descr": ctx.tools_description,
        "max_steps": config_dict["max_steps"],
        "skills_list": skills_list,
        "system_prompt_id": config_dict["system_prompt_id"],
        "use_server_subagents": config_dict.get("use_server_subagents", False),
    }

    state = State(**state_data)
    save_state(state)

    messages = [
        {
            "role": "user",
            "content": message,
            "id": generate_message_id_func("m"),
        }
    ]

    response = run_agent(messages, state, ctx)

    if config_dict.get("dump_storage"):
        cur_dir = os.getcwd()
        dump_storage(cur_dir, indent=2)

    print(f"\nПуть к файлу с логами: {ctx.logging_path}\n")

    return response


def main() -> Any:
    """
    Входная точка CLI: парсинг аргументов, загрузка конфига,
    создание RunContext и запуск агента.
    """
    config_dict = load_config(arg_parser.run_args.config)
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    query = arg_parser.run_args.query
    verbose = arg_parser.run_args.verbose

    return run(query, ctx, verbose=verbose)


if __name__ == "__main__":
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
