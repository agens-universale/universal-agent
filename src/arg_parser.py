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
    required=True,
    help="Запрос к агенту (обязательный параметр)",
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
    "-n",
    "--new",
    action="store_true",
    help="Начать тред заново: удалить существующие данные треда (только с -t/--thread-id)",
)

root_box_group = parser.add_mutually_exclusive_group()
root_box_group.add_argument(
    "--root-box",
    type=_parse_bool_cli,
    choices=[True, False],
    help=(
        "Включить/выключить root_box (true|false): ограничение файловых "
        "изменений launch_root и command_moderation.root_box.extra_write_paths"
    ),
)
root_box_group.add_argument(
    "--no-root-box",
    action="store_true",
    help=(
        "Явно отключить root_box (эквивалент --root-box false); "
        "имеет приоритет над config.yml"
    ),
)

parser.add_argument(
    "--command-moderation",
    choices=["allow", "ask", "deny"],
    help=(
        "Режим execute_command/run_python_code: allow без judge, "
        "ask с подтверждением опасного, deny блокирует опасное"
    ),
)


parsed_args, unknown_args = parser.parse_known_args()
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
