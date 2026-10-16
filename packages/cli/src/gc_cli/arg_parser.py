import argparse
import sys
from typing import Any, Dict

from gc_core.config_modules.cli_args import (
    coerce_cli_value,
    parse_override_args,
    setting_flag_overrides,
)
from gc_core.config_modules.model_registry import LEVEL_VALUES

run_args = dict()
extra_config_overrides: Dict[str, Any] = {}


def _parse_bool_cli(raw: str) -> bool:
    value = str(raw).strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("Ожидается true или false")


# Разбор ``--путь value`` — общий с gc-acp и gc-chat.
_coerce_value = coerce_cli_value
_parse_unknown_args = parse_override_args


parser = argparse.ArgumentParser(
    # Без сокращений: «--mod x» не должен молча стать --model, а «--agent …» —
    # флагом --agentsmd-override. Пути настроек (--agent.max_steps) — целиком.
    allow_abbrev=False,
    description="CLI-агент для взаимодействия с AI-сервером",
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Примеры использования:
  agent -q "напиши функцию сортировки списка"
  agent --query "как оптимизировать этот код?" --verbose
  agent -q "..." --model CodeChat --agent.max_steps 50

Любая настройка — путём через точку: --agent.max_steps 50, --security.read_guard.mode ask

Конфиг (агент не запускается):
  gc-cli config explain [KEY ...]   значение и источник настроек
  gc-cli config validate            проверить, что конфиг собирается
  gc-cli config schema              JSON Schema конфига
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
    "--sync-config",
    action="store_true",
    dest="sync_config",
    help="Обновить config.yml до текущей версии (новые поля, переименования; старый файл — в бэкап) и выйти",
)

parser.add_argument(
    "--reset-config",
    action="store_true",
    dest="reset_config",
    help=(
        "Сбросить config.yml к актуальному пакетному умолчанию (интерактивное "
        "y/N-подтверждение; старый файл — в бэкап; профили и сессионные "
        "оверрайды не трогает) и выйти"
    ),
)

parser.add_argument(
    "--init",
    action="store_true",
    help="Сгенерировать AGENTS.md сканом репозитория и выйти (агент не запускается)",
)

parser.add_argument(
    "--check-update",
    action="store_true",
    dest="check_update",
    help="Проверить, доступна ли новая версия агента (без установки) и выйти",
)

parser.add_argument(
    "--update",
    action="store_true",
    dest="update",
    help="Обновить агента (detached: ждёт завершения текущего процесса, затем uv tool install) и выйти",
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
    # Объявлен явно: необъявленный `--image PATH` молча ушёл бы в переопределения
    # конфига (`_parse_unknown_args`), и запрос ушёл бы без картинки.
    "--image",
    metavar="PATH",
    action="append",
    default=None,
    dest="images",
    help=(
        "Приложить изображение к запросу -q; ключ повторяется, не больше "
        "images.max_per_message. С -t картинка хранится вместе с тредом"
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
        "Явно отключить write_guard (эквивалент --write-guard false); имеет приоритет над config.yml"
    ),
)

parser.add_argument(
    "--command-moderation",
    choices=["allow", "ask", "deny"],
    help=(
        "Режим exec_command/exec_python: allow без judge, ask с подтверждением опасного, deny блокирует опасное"
    ),
)

read_guard_group = parser.add_mutually_exclusive_group()
read_guard_group.add_argument(
    "--read-guard",
    type=_parse_bool_cli,
    choices=[True, False],
    help=(
        "Включить/выключить read_guard (true|false): ограничение чтения файлов по allowed_read_roots/denied_*"
    ),
)
read_guard_group.add_argument(
    "--no-read-guard",
    action="store_true",
    help=(
        "Явно отключить read_guard (эквивалент --read-guard false); имеет приоритет над config.yml"
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

parser.add_argument(
    "--model",
    type=str,
    default=None,
    dest="model",
    help=(
        "Модель на этот запуск (agent.model). При непустом реестре моделей — "
        "легаси-форс: уровни и алиасы отключаются на этот запуск"
    ),
)

parser.add_argument(
    "--completions_url",
    "--completions-url",
    type=str,
    default=None,
    dest="completions_url",
    help=(
        "Адрес chat/completions на этот запуск (backend.completions_url); "
        "при непустом реестре моделей — легаси-форс, как у --model"
    ),
)

parser.add_argument(
    "--profile",
    type=str,
    default=None,
    help=(
        "Активировать профиль <имя> поверх указателя profile: из config.yml "
        "(~/.gc/profiles/ или <project>/.gc/profiles/)"
    ),
)

parser.add_argument(
    "--level",
    choices=list(LEVEL_VALUES),
    default=None,
    help=(
        "Активный уровень модели на этот запуск (перекрывает config.yml и профиль); "
        "auto — уровень меняется автоматически по ходу работы"
    ),
)

parser.add_argument(
    "--agentsmd-override",
    action="store_true",
    help=(
        "Только TUI: оставить лишь корневой AGENTS.md (L3), отбросив глобальный и .gc/AGENTS.md слои"
    ),
)

parser.add_argument(
    "--structured-output-format",
    type=str,
    default=None,
    dest="structured_output_format",
    metavar="ПУТЬ",
    help=(
        "Путь к файлу с описанием формы. Относительный считается от "
        "текущей папки запуска. После итога хода — принятой прозы или "
        "аргументов turn_finish как есть — переложить его в эту форму "
        "(текст описания, не JSON Schema) одним служебным запросом и "
        "отдать этот текст наружу. Запрос идёт моделью компакции "
        "(потребитель summary, привязка assign.summary). "
        "Без флага ответ агента печатается как раньше"
    ),
)

parser.add_argument(
    # Оба написания — один и тот же параметр. Подчёркнутая форма объявлена
    # намеренно: без неё она проваливалась бы в generic-механизм неизвестных
    # `--param value`, а тот прогоняет значение через yaml.safe_load, и трейс
    # `00123` тихо становился числом 83, `2026-07-27` — датой, `on` — True.
    # Объявленный флаг с type=str берёт значение буквально.
    "--trace-id",
    "--trace_id",
    type=str,
    default=None,
    dest="trace_id",
    help=(
        "Внешний trace_id для корреляции с оркестратором: попадает в имя лог-файла, "
        "в записи логов, в заголовок к бэкенду модели и в MCP-заголовки. "
        "Принимается в обоих написаниях (--trace-id и --trace_id). "
        "Не задан — ничего не меняется"
    ),
)


#: `gc-cli config …` — отдельная команда (gc_core.config_command) со своим
#: разбором argv: её уводит `cli.main` до старта агента. Парсер агента разбирается
#: на импорте модуля, поэтому здесь он её не трогает — иначе импорт `gc_cli.cli`
#: падал бы на «Нужен -q/--query» раньше, чем `main` успеет её увести.
CONFIG_COMMAND = sys.argv[1:2] == ["config"]


#: Флаги-действия: без -q/--query запуск осмыслен только с одним из них.
_STANDALONE_ACTIONS = (
    "init_config",
    "init",
    "sync_config",
    "reset_config",
    "check_update",
    "update",
    "query",
)

#: Несовместимые пары флагов и текст ошибки; порядок — порядок проверки.
_CONFLICTS = (
    (
        "init_config",
        "query",
        "--init-config и -q/--query нельзя вместе: --init-config только настраивает и выходит.",
    ),
    (
        "sync_config",
        "query",
        "--sync-config и -q/--query нельзя вместе: --sync-config только обновляет файл и выходит.",
    ),
    (
        "reset_config",
        "query",
        "--reset-config и -q/--query нельзя вместе: --reset-config только сбрасывает конфиг и выходит.",
    ),
    (
        "init_config",
        "sync_config",
        "--init-config и --sync-config нельзя вместе: используйте одно из двух.",
    ),
    (
        "reset_config",
        "sync_config",
        "--reset-config и --sync-config нельзя вместе: используйте одно из двух.",
    ),
    (
        "reset_config",
        "init_config",
        "--reset-config и --init-config нельзя вместе: используйте одно из двух.",
    ),
    (
        "check_update",
        "query",
        "--check-update и -q/--query нельзя вместе: --check-update только проверяет и выходит.",
    ),
    (
        "update",
        "query",
        "--update и -q/--query нельзя вместе: --update только запускает обновление и выходит.",
    ),
    (
        "update",
        "check_update",
        "--update и --check-update нельзя вместе: используйте одно из двух.",
    ),
)


def _check_combinations(parsed_args: argparse.Namespace) -> None:
    if not any(
        getattr(parsed_args, name, False) for name in _STANDALONE_ACTIONS
    ):
        parser.error(
            "Нужен -q/--query "
            "(или --init-config для конфига, --sync-config для обновления конфига, "
            "--reset-config для сброса конфига, --init для AGENTS.md, "
            "--check-update/--update для обновления агента)."
        )
    for first, second, message in _CONFLICTS:
        if getattr(parsed_args, first) and getattr(parsed_args, second):
            parser.error(message)
    if parsed_args.new and not parsed_args.thread_id:
        parser.error(
            "Аргумент -n/--new можно использовать только вместе с -t/--thread-id."
        )


parsed_args, unknown_args = parser.parse_known_args(
    [] if CONFIG_COMMAND else None
)
if not CONFIG_COMMAND:
    _check_combinations(parsed_args)
run_args = parsed_args
extra_config_overrides = _parse_unknown_args(list(unknown_args))
extra_config_overrides.update(setting_flag_overrides(parsed_args))


if __name__ == "__main__":
    print(run_args)
    if extra_config_overrides:
        print("Extra config overrides from CLI:")
        for k, v in extra_config_overrides.items():
            print(f"  {k} = {v!r}")
