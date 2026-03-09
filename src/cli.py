import os
from typing import Any

import arg_parser
from config import load_config, build_auth_connector
from processing_responses import run_agent
from run_context import RunContext, create_run_context
from states import State, save_state, dump_storage
from thread_store import (
    ThreadSession,
)
from tools.skills.registry import get_skills_registry
from utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


def run(message: str, ctx: RunContext, *, thread_id: str | None = None, verbose: bool | None = None) -> Any:
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
        "agent_md_path": config_dict.get("agent_md_path"),
    }

    state = State(**state_data)
    save_state(state)

    new_user_message = {
        "role": "user",
        "content": message,
        "id": generate_message_id_func("m"),
    }
    messages = [new_user_message]

    thread_session: ThreadSession | None = None
    persist_messages_callback = None
    get_full_history_messages = None
    if thread_id is not None:
        thread_session = ThreadSession.from_config(
            config_dict=config_dict, thread_id=thread_id
        )
        state.previous_response_id = thread_session.previous_response_id
        messages = thread_session.build_initial_messages(new_user_message)
        thread_session.set_runtime_messages(state.messages)

        def _persist_thread_batch(batch_messages: list[dict]) -> None:
            thread_session.persist_batch(
                batch_messages, previous_response_id=state.previous_response_id
            )

        persist_messages_callback = _persist_thread_batch
        get_full_history_messages = thread_session.get_full_history_messages

    response = run_agent(
        messages,
        state,
        ctx,
        get_full_history_messages=get_full_history_messages,
        persist_messages_callback=persist_messages_callback,
    )

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

    # Переопределяем значения из config.yml аргументами командной строки вида
    # --param value / --param=value / --flag.
    # Значения из CLI имеют приоритет над конфигом.
    if getattr(arg_parser, "extra_config_overrides", None):
        config_dict.update(arg_parser.extra_config_overrides)
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    query = arg_parser.run_args.query
    verbose = arg_parser.run_args.verbose
    thread_id = arg_parser.run_args.thread_id

    return run(query, ctx, thread_id=thread_id, verbose=verbose)


if __name__ == "__main__":
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
