from typing import Any

import arg_parser
from agent_executor import AgentExecutor
from config import load_config, build_auth_connector
from run_context import RunContext, create_run_context


def run(
    message: str,
    ctx: RunContext,
    *,
    thread_id: str | None = None,
    verbose: bool | None = None,
) -> Any:
    """
    Основная функция одного запуска агента.
    Может вызываться как из CLI, так и из REST‑обёрток.
    """
    return AgentExecutor(ctx).run(
        message, thread_id=thread_id, verbose=verbose
    )


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

    executor = AgentExecutor(ctx)
    return executor.run(query, thread_id=thread_id, verbose=verbose)


if __name__ == "__main__":
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
