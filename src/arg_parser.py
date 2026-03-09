import argparse
from typing import Any, Dict, List

import yaml

run_args = dict()
extra_config_overrides: Dict[str, Any] = {}


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


parsed_args, unknown_args = parser.parse_known_args()
run_args = parsed_args
extra_config_overrides = _parse_unknown_args(list(unknown_args))


if __name__ == "__main__":
    print(run_args)
    if extra_config_overrides:
        print("Extra config overrides from CLI:")
        for k, v in extra_config_overrides.items():
            print(f"  {k} = {v!r}")
