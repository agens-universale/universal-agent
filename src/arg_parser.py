import argparse
from typing import Any, Dict, List

import yaml

run_args = dict()
extra_config_overrides: Dict[str, Any] = {}


def _parse_bool_cli(raw: str) -> bool:
    value = str(raw).strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("Ожидается true или false")


def _coerce_value(raw: str) -> Any:
    """
    Преобразовать строковое значение из CLI к более подходящему типу.

    Используем yaml.safe_load, чтобы поддержать числа, bool и т.п.
    В случае неудачи или получения None возвращаем исходную строку.
    """
    try:
        value = yaml.safe_load(raw)
    except Exception:
        return raw
    return raw if value is None else value


def _parse_unknown_args(args: List[str]) -> Dict[str, Any]:
    """
    Разобрать дополнительные аргументы вида:
    --param value
    --param=value
    --flag

    Возвращает словарь переопределений для config_dict.
    """
    overrides: Dict[str, Any] = {}
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("--"):
            i += 1
            continue

        key_part = token[2:]
        value: Any = True

        if "=" in key_part:
            key, raw_val = key_part.split("=", 1)
            value = _coerce_value(raw_val)
        else:
            key = key_part
            if i + 1 < len(args) and not args[i + 1].startswith("--"):
                raw_val = args[i + 1]
                value = _coerce_value(raw_val)
                i += 1

        if key:
            overrides[key] = value

        i += 1

    return overrides


parser = argparse.ArgumentParser(
    description="CLI-агент для взаимодействия с AI-сервером",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Примеры использования:
  agent -q "напиши функцию сортировки списка"
  agent --query "как оптимизировать этот код?" --verbose
  agent -q "..." --model CodeChat --max_steps 50
        """,
)

parser.add_argument(
    "-q",
    "--query",
    type=str,
    required=False,
    help="Запрос к агенту (обязателен, кроме --init-config)",
)

parser.add_argument(
    "--init-config",
    action="store_true",
    help="Создать ~/.gc/config.yml из шаблона и выйти (агент не запускается)",
)

parser.add_argument(
    "--init",
    action="store_true",
    help="Сгенерировать AGENTS.md сканом репозитория и выйти (агент не запускается)",
)

parser.add_argument(
    "-v",
    "--verbose",
    action="store_true",
    help="Выводить подробную информацию о запросе",
)

parser.add_argument(
    "-T",
    "--technical",
    action="store_true",
    dest="wrap_technical",
    help='Обернуть запрос в теги <technical type="technical">…</technical>',
)

parser.add_argument(
    "-s",
    "--skill",
    metavar="SKILL_NAME",
    type=str,
    default=None,
    help=(
        "Добавить техническое указание: решать запрос через skill с данным "
        "именем; при отсутствии — похожее имя (опечатка), если описание "
        "подходит к задаче; иначе сообщить об ошибке и остановиться"
    ),
)

parser.add_argument(
    "--config",
    type=str,
    help="Путь к кастомному конфигурационному файлу",
)

parser.add_argument(
    "-t",
    "--thread-id",
    type=str,
    help="Идентификатор треда для продолжения диалога",
)

parser.add_argument(
    "-a",
    "--auto-mcp",
    action="store_true",
    help=(
        "Поднять HTTP/SSE MCP-daemon'ы из recommended_mcp/ перед запуском "
        "(если каталог есть в cwd); по завершении gc-cli гасятся только "
        "процессы, запущенные этим вызовом"
    ),
)

parser.add_argument(
    "-n",
    "--new",
    action="store_true",
    help="Начать тред заново: удалить существующие данные треда (только с -t/--thread-id)",
)

write_guard_group = parser.add_mutually_exclusive_group()
write_guard_group.add_argument(
    "--write-guard",
    type=_parse_bool_cli,
    choices=[True, False],
    help=(
        "Включить/выключить write_guard (true|false): ограничение файловых "
        "изменений launch_root и write_guard.extra_paths"
    ),
)
write_guard_group.add_argument(
    "--no-write-guard",
    action="store_true",
    help=(
        "Явно отключить write_guard (эквивалент --write-guard false); "
        "имеет приоритет над config.yml"
    ),
)

parser.add_argument(
    "--command-moderation",
    choices=["allow", "ask", "deny"],
    help=(
        "Режим exec_command/exec_python: allow без judge, "
        "ask с подтверждением опасного, deny блокирует опасное"
    ),
)

read_guard_group = parser.add_mutually_exclusive_group()
read_guard_group.add_argument(
    "--read-guard",
    type=_parse_bool_cli,
    choices=[True, False],
    help=(
        "Включить/выключить read_guard (true|false): ограничение чтения файлов "
        "по allowed_read_roots/denied_*"
    ),
)
read_guard_group.add_argument(
    "--no-read-guard",
    action="store_true",
    help=(
        "Явно отключить read_guard (эквивалент --read-guard false); "
        "имеет приоритет над config.yml"
    ),
)

parser.add_argument(
    "--read-guard-mode",
    choices=["allow", "ask", "deny"],
    help=(
        "Режим read_guard: allow выполняет без проверок, "
        "ask запрашивает подтверждение для подозрительного, deny блокирует"
    ),
)


parsed_args, unknown_args = parser.parse_known_args()
if not parsed_args.init_config and not getattr(parsed_args, "init", False) and not parsed_args.query:
    parser.error("Нужен -q/--query (или --init-config для конфига, --init для AGENTS.md).")
if parsed_args.new and not parsed_args.thread_id:
    parser.error("Аргумент -n/--new можно использовать только вместе с -t/--thread-id.")
run_args = parsed_args
extra_config_overrides = _parse_unknown_args(list(unknown_args))


if __name__ == "__main__":
    print(run_args)
    if extra_config_overrides:
        print("Extra config overrides from CLI:")
        for k, v in extra_config_overrides.items():
            print(f"  {k} = {v!r}")
