from typing import Any

import arg_parser
from agent_executor import AgentExecutor
from config import (
    apply_cli_overrides,
    build_auth_connector,
    load_config,
)
from message_policy.user_message_policy import (
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)
from run_context import RunContext, create_run_context


def run(
    messages: list[str],
    ctx: RunContext,
    *,
    thread_id: str | None = None,
    start_new_thread: bool = False,
    verbose: bool | None = None,
) -> Any:
    """
    Основная функция одного запуска агента.
    Может вызываться как из CLI, так и из REST‑обёрток.

    messages: непустой список строк — каждая уходит отдельным user-сообщением
    (один запрос передавайте как ``[текст]``).
    """
    return AgentExecutor(ctx).run(
        messages,
        thread_id=thread_id,
        start_new_thread=start_new_thread,
        verbose=verbose,
    )


def main() -> Any:
    """
    Входная точка CLI: парсинг аргументов, загрузка конфига,
    создание RunContext и запуск агента.
    """
    config_dict = load_config(arg_parser.run_args.config)

    # Единая точка применения CLI-override поверх config.yml.
    apply_cli_overrides(
        config_dict,
        extra_overrides=getattr(arg_parser, "extra_config_overrides", None),
        root_box_enabled=arg_parser.run_args.root_box,
        no_root_box=arg_parser.run_args.no_root_box,
        command_moderation_mode=arg_parser.run_args.command_moderation,
    )
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    user_parts: list[str] = [arg_parser.run_args.query]
    if arg_parser.run_args.wrap_technical:
        user_parts[0] = wrap_technical_user_content(user_parts[0])
    skill_name = (arg_parser.run_args.skill or "").strip()
    if skill_name:
        user_parts.append(wrap_skill_resolution_technical_instruction(skill_name))

    verbose = arg_parser.run_args.verbose
    thread_id = arg_parser.run_args.thread_id
    start_new_thread = arg_parser.run_args.new

    executor = AgentExecutor(ctx)
    return executor.run(
        user_parts,
        thread_id=thread_id,
        start_new_thread=start_new_thread,
        verbose=verbose,
    )


if __name__ == "__main__":
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
