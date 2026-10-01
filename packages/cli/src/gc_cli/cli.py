import os
import sys
from typing import Any, NamedTuple

from gc_core import config_sync, onboarding
from gc_core.agent_executor import AgentExecutor
from gc_core.attachments.model import (
    ORIGIN_USER,
    Attachment,
    chip_text,
    user_label,
)
from gc_core.attachments.normalize import REASON_TOO_LARGE, NormalizeError
from gc_core.attachments.numbering import read_image_seq
from gc_core.attachments.store import ImageStore, amake_attachment
from gc_core.config import (
    _bootstrap_user_config,
    apply_cli_overrides,
    build_auth_connector,
    cli_forces_legacy_model,
    load_config,
    load_config_ex,
    resolve_config_path,
    user_config_path,
)
from gc_core.config_modules.images import read_images_config
from gc_core.message_policy.user_message_policy import (
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)
from gc_core.run_context import RunContext, acreate_run_context
from gc_core.thread_store import resolve_threads_dir
from gc_core.thread_store_file import FileThreadStore
from gc_core.updater import check as updater_check
from gc_core.updater import runner as updater_runner
from gc_core.updater import source as updater_source
from gc_core.utils.json_text import format_json_for_display
from gc_core.utils.logging_module import human_console_enabled
from recommended_mcp.runtime.daemon_autostart import (
    ensure_mcp_daemons,
    shutdown_mcp_daemons,
)

from gc_cli import arg_parser
from gc_cli.structured_output import (
    StructuredOutputError,
    aemit_structured_result,
    read_format_description,
)


def _ensure_project_gc_dir() -> None:
    """Создаёт <cwd>/.gc/ при старте gc-cli. Делегирует общему хелперу
    (тот же вызывает gc-ua) — см. env_vars.ensure_project_gc_dir."""
    from gc_core.config_modules.env_vars import ensure_project_gc_dir

    ensure_project_gc_dir()


def _ensure_user_skills_dir() -> None:
    """
    Создаёт <gc_data_dir>/skills/ при старте gc-cli (идемпотентно).
    В отличие от project-слоя, user-слой создаём заранее: пользователь точно
    рано или поздно захочет добавить свой скилл, и видеть готовую папку
    в `~/.local/share/gc/` (или `~/.gc/` на Windows) — это discoverability.
    На PermissionError/OSError тихо пропускает.
    """
    try:
        from gc_core.config_modules.env_vars import gc_paths

        os.makedirs(gc_paths()["data"] / "skills", exist_ok=True)
    except OSError:
        pass


async def arun(
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
    return await AgentExecutor(ctx).arun(
        messages,
        thread_id=thread_id,
        start_new_thread=start_new_thread,
        verbose=verbose,
    )


async def _resolve_forced_skill(
    skill_name: str, config_dict: dict
) -> str | None:
    """Resolve the -s/--skill flag deterministically (pack-aware).

    Returns the exact canonical skill id when resolved; the raw name as a
    soft fallback when it can't be resolved (unresolvable / ambiguous, or the
    catalog can't be built); or None when no -s was given. The agent always
    starts — an unresolved -s is a hint, not a hard contract — but the user
    is loudly warned on stderr so an explicitly requested -s is never
    silently dropped.
    """
    name = (skill_name or "").strip()
    if not name:
        return None
    from gc_core.tools.skills.registry import get_skills_registry
    from gc_core.tools.skills.skill_arg_resolver import (
        Failed,
        resolve_skill_arg,
    )

    try:
        catalog = await get_skills_registry(config_dict).skill_catalog()
    except Exception as exc:  # surface as warning, not traceback
        print(
            f"warning: не удалось построить каталог скиллов для -s ({exc}); "
            f"агент стартует, '-s {name}' передан агенту как есть.",
            file=sys.stderr,
        )
        return name

    res = resolve_skill_arg(name, catalog)
    if isinstance(res, Failed):
        msg = (
            f"warning: -s '{name}' не разрешён детерминированно "
            f"({res.reason}); агент стартует, имя передано агенту как есть."
        )
        if res.suggestions:
            msg += " Возможно: " + ", ".join(res.suggestions) + "."
        print(msg, file=sys.stderr)
        return name
    if res.note:
        print(f"note: {res.note}", file=sys.stderr)
    return res.canonical_id


#: Отказы `--image` — в stderr, до старта агента. По смыслу — те же, что отказы
#: вставки в футере TUI.
IMAGE_TOO_MANY = "[gc] --image: не больше {n} изображений в одном сообщении (передано {count})"
IMAGE_UNREADABLE = "[gc] --image {path}: файл не найден или недоступен"
IMAGE_NOT_AN_IMAGE = "[gc] --image {path}: это не изображение"
IMAGE_TOO_LARGE = "[gc] --image {path}: изображение слишком большое"


class ImageArgError(ValueError):
    """Отказ `--image`: текст готов для stderr."""


def _first_image_number(
    threads_dir: str, thread_id: str | None, start_new_thread: bool
) -> int:
    """Номер первой картинки запроса — сквозной по треду (спека §4).

    С `-t` — следующий за `runtime.image_seq` треда; без `-t` и с `-n` — 1: тред
    либо не пишется, либо начинается заново. Система `cli` — та же, что ставит
    исполнитель по умолчанию (`AgentExecutor.arun`).
    """
    if not thread_id or start_new_thread:
        return 1
    return (
        read_image_seq(
            FileThreadStore(threads_dir).load_meta("cli", thread_id)
        )
        + 1
    )


async def _astage_cli_images(
    paths: list[str], store: ImageStore, *, first_n: int, max_per_message: int
) -> list[Attachment]:
    """Файлы `--image` → вложения в кармане процесса, по порядку, с номерами от `first_n`.

    Отказ — `ImageArgError` с готовым текстом. Один и тот же файл дважды — одно
    вложение: вложения сообщения уникальны по хешу (спека §4).
    """
    if len(paths) > max_per_message:
        raise ImageArgError(
            IMAGE_TOO_MANY.format(n=max_per_message, count=len(paths))
        )
    out: list[Attachment] = []
    for path in paths:
        try:
            att = await amake_attachment(
                store,
                path,
                origin=ORIGIN_USER,
                label=user_label(first_n + len(out)),
            )
        except OSError as exc:
            raise ImageArgError(IMAGE_UNREADABLE.format(path=path)) from exc
        except NormalizeError as exc:
            text = (
                IMAGE_TOO_LARGE
                if exc.reason == REASON_TOO_LARGE
                else IMAGE_NOT_AN_IMAGE
            )
            raise ImageArgError(text.format(path=path)) from exc
        if all(prev.id != att.id for prev in out):
            out.append(att)
    return out


class _CliImages(NamedTuple):
    """Картинки `--image` запуска: карман процесса, вложения и номер первой плашки."""

    store: ImageStore
    images: list[Attachment]
    first_n: int


async def _aprepare_cli_images(
    config_dict: dict,
    image_paths: list[str],
    *,
    thread_id: str | None,
    start_new_thread: bool,
) -> _CliImages | None:
    """Карман процесса и картинки `--image` — до стампа и до сборки контекста: отказ по
    файлу уходит раньше MCP-дискавери и не «сжигает» notice «агент обновлён». Карманы
    мёртвых процессов подметаем на каждом старте, есть картинки или нет; уборка — не
    условие запуска: ошибка диска её пропускает, как в TUI. Каталог тредов нужен только
    номеру первой плашки — без `--image` к нему не обращаемся. None — отказ: текст уже в
    stderr, карман снят."""
    store = ImageStore.for_config(config_dict)
    try:
        store.sweep_dead_staging()
    except OSError:
        pass
    if not image_paths:
        return _CliImages(store, [], 1)
    first_n = _first_image_number(
        resolve_threads_dir(config_dict), thread_id, start_new_thread
    )
    try:
        images = await _astage_cli_images(
            image_paths,
            store,
            first_n=first_n,
            max_per_message=read_images_config(config_dict).max_per_message,
        )
    except ImageArgError as exc:
        print(str(exc), file=sys.stderr)
        store.clear_own_staging()
        return None
    return _CliImages(store, images, first_n)


def _cli_user_parts(
    query: str,
    images: list[Attachment],
    *,
    first_n: int,
    wrap_technical: bool,
    forced_skill_id: str | None,
) -> tuple[list[str], list[list[Attachment] | None] | None]:
    """Сообщения запуска и параллельный им список вложений для `arun`.

    Картинки — вложения к сообщению `-q`: его текст начинается с плашек
    `[Изображение #N]`. При `-T` обёртка ложится поверх плашек и текста — это всё
    ещё сообщение `-q`. Указание `-s` — отдельное техническое сообщение без вложений.
    """
    text = query
    if images:
        chips = " ".join(chip_text(first_n + i) for i in range(len(images)))
        text = f"{chips} {query}"
    if wrap_technical:
        text = wrap_technical_user_content(text)
    parts = [text]
    if forced_skill_id:
        parts.append(
            wrap_skill_resolution_technical_instruction(forced_skill_id)
        )
    if not images:
        return parts, None
    return parts, [list(images), *([None] * (len(parts) - 1))]


def _interactive() -> bool:
    """TTY на входе и выходе → можно запускать интерактивный визард."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_init() -> int:
    """`gc-cli --init-config`: настроить конфиг (интерактивно на TTY) и выйти."""
    path = user_config_path()
    just_created = _bootstrap_user_config()
    # «Чинилка» обязана работать именно тогда, когда конфиг сломан: лестница
    # ступеней и текст деградации — общие с gc-ua (onboarding, один дом).
    try:
        loaded = onboarding.load_config_for_repair()
    except onboarding.RepairRefused as exc:
        # M10: ступень шаблона при живом указателе профиля — запись мастера
        # обречена (гейт судит стек следующего старта), и вести человека через
        # все вопросы с вводом токена к отказу записи нечестно. Текст готов у
        # лестницы — печатаем как есть.
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        # Лестница исчерпана: не собирается и шаблон — сломан слой, в который
        # мастер не пишет (оверлей). Чинить его мастером нечем: то же объяснение,
        # что на обычном старте, и код 1 — не трейсбек.
        print(
            onboarding.explain_broken_config(str(path), exc), file=sys.stderr
        )
        return 1
    config_dict = loaded.config
    if loaded.message:
        # Мастер gc-cli плоский, экран не занимает — stderr человек видит.
        print(f"[gc] {loaded.message}", file=sys.stderr)
    if _interactive():
        from gc_cli import onboarding_wizard

        configured = onboarding_wizard.run(
            config_dict,
            path,
            review_all=True,
            io=onboarding_wizard.default_io(),  # без mcp — gc-cli не ставит MCP
            # Ступень «без профиля» значит, что конфиг собрался только со снятым
            # профилем: финал обязан снять и указатель, иначе записать починку
            # некуда. Но снять его мастер может не всегда (указатель в managed-слое),
            # и судит об этом лестница: своя редакция признака обещала бы человеку
            # деактивацию, которую писатель не сделает, — а пустой якорь в базу
            # уже уехал бы (ревью D3).
            deactivate_profile=loaded.deactivate_profile,
        )
        # run() отдаёт False не только на трейсбеке, но и когда человек прошёл
        # мастер, а конфиг всё равно остался ненастроенным (пропущенные поля,
        # отказ записи, файл, который не удалось перечитать) — код возврата
        # обязан отражать это, а не рапортовать успех при честном stderr рядом.
        return 0 if configured else 1
    else:
        # Гайд знает ступень лестницы: на деградировавшей «настроен — можно
        # запускать» врало бы человеку, чей следующий старт падает.
        print(
            onboarding.setup_guide(
                str(path),
                config_dict,
                just_created=just_created,
                repair=loaded,
            ),
            file=sys.stderr,
        )
    return 0


def _run_sync_config() -> int:
    """`gc-cli --sync-config`: обновить config.yml по свежему шаблону и выйти."""
    from pathlib import Path

    from gc_core.config import _default_config_path
    from gc_core.config_modules.env_vars import project_local_config_path

    config_arg = arg_parser.run_args.config
    target = Path(config_arg) if config_arg else user_config_path()
    bundled = Path(_default_config_path()).resolve()

    # I5: project-local overlay — частичный файл, sync его не трогает
    project_local = project_local_config_path()
    if project_local and Path(project_local).resolve() == target.resolve():
        print(
            "[gc] project-local .gc/config.yml — частичный overlay, sync его не обновляет\n"
            "     (обновляйте глобальный config.yml).",
            file=sys.stderr,
        )
        return 1

    # M9a: раздельные сообщения для «не создан» и «это bundled-шаблон»
    if not target.is_file():
        print(
            f"[gc] config.yml не найден (ещё не создан) — {target}.\n"
            f"     Сначала настройте агента: gc-cli --init-config",
            file=sys.stderr,
        )
        return 1
    if target.resolve() == bundled:
        print(
            "[gc] нечего обновлять: target — пакетный шаблон config.default.yml, его не правим.",
            file=sys.stderr,
        )
        return 1
    try:
        report = config_sync.sync_config(target)
    except ValueError as exc:
        print(f"[gc] config.yml НЕ обновлён: {exc}", file=sys.stderr)
        return 1
    lines = [
        f"[gc] ✓ config.yml обновлён по образцу текущей версии: {target}",
        f"     перенесено: {report.transferred} · переименовано: {report.renamed}",
    ]
    if report.removed:
        lines.append(
            f"     устаревших убрано: {', '.join(report.removed)} (в бэкапе)"
        )
    if report.unknown:
        lines.append(
            f"     неизвестных ключей: {', '.join(report.unknown)} (в бэкапе)"
        )
    if report.reenter:
        lines.append(
            f"     задайте заново (формат изменился): {', '.join(report.reenter)}"
        )
    lines.append(f"     бэкап: {report.backup_path}")
    lines.append(
        "     личные комментарии из старого файла не переносятся — они в бэкапе."
    )
    print("\n".join(lines), file=sys.stderr)
    return 0


def _run_reset_config() -> int:
    """`gc-cli --reset-config`: подменить config.yml пакетным умолчанием (спека §11).

    Файлы профилей (`~/.gc/profiles/*`) и сессионные оверрайды не трогает — заменяет
    ровно тот файл, что был бы базой запуска (вместе с его указателем `profile:`:
    выбранный профиль после сброса деактивирован). Интерактивное y/N-подтверждение;
    без TTY — честный отказ (не молчаливое да и не молчаливый отказ)."""
    from pathlib import Path

    from gc_core.config import _default_config_path
    from gc_core.config_modules.env_vars import project_local_config_path

    config_arg = arg_parser.run_args.config
    target = Path(config_arg) if config_arg else user_config_path()
    bundled = Path(_default_config_path()).resolve()

    project_local = project_local_config_path()
    if project_local and Path(project_local).resolve() == target.resolve():
        print(
            "[gc] project-local .gc/config.yml — частичный overlay, сброс к пакетному "
            "умолчанию к нему неприменим (сбрасывайте глобальный config.yml).",
            file=sys.stderr,
        )
        return 1
    if not target.is_file():
        print(
            f"[gc] config.yml не найден (ещё не создан) — {target}.\n"
            f"     Сначала настройте агента: gc-cli --init-config",
            file=sys.stderr,
        )
        return 1
    if target.resolve() == bundled:
        print(
            "[gc] нечего сбрасывать: target — пакетный шаблон config.default.yml, его не правим.",
            file=sys.stderr,
        )
        return 1
    if not _interactive():
        print(
            "[gc] --reset-config требует интерактивного подтверждения (TTY) — запустите в терминале и подтвердите y/N.",
            file=sys.stderr,
        )
        return 1
    print(
        f"[gc] Сбросить {target} к пакетному умолчанию? Текущий файл уйдёт в бэкап. [y/N]: ",
        file=sys.stderr,
        end="",
    )
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer not in ("y", "yes"):
        print("[gc] отменено", file=sys.stderr)
        return 0
    try:
        report = config_sync.reset_config(target)
    except (ValueError, OSError) as exc:
        print(f"[gc] config.yml НЕ сброшен: {exc}", file=sys.stderr)
        return 1
    print(
        f"[gc] ✓ config.yml сброшен к пакетному умолчанию: {target}\n     бэкап: {report.backup_path}",
        file=sys.stderr,
    )
    return 0


def _updater_source_or_hint():
    """Источник обновления либо None — с объяснением в stderr.

    Общая часть `--check-update` и `--update`: обе команды одинаково молчат про
    dev-установку и одинаково отдают 0.
    """
    src = updater_source.resolved_source()
    if src is None:
        print(
            "[gc] обновление недоступно для этой установки — обновляйтесь через git (dev-установка)",
            file=sys.stderr,
        )
    return src


def _run_check_update() -> int:
    """`gc-cli --check-update`: разовая проверка обновлений и выход.

    Работает и при ``update.check: off`` в config.yml — это ручная команда,
    троттлинг авто-чека её не касается.
    """
    src = _updater_source_or_hint()
    if src is not None:
        result = updater_check.check_for_update(src)
        # ASCII-only маркеры ([update]\[ok]\[--]), не $ok/$warn-глифы (✓\⚠\—):
        # консоль, в которую печатает CLI, может быть не-UTF8 (напр. Windows
        # cp1251) — такие глифы вне её кодовой страницы валят print() с
        # UnicodeEncodeError.
        if result.status == "update":
            print(f"[gc] [update] {result.detail}", file=sys.stderr)
        elif result.status == "current":
            print(f"[gc] [ok] {result.detail}", file=sys.stderr)
        else:
            print(f"[gc] [--] {result.detail}", file=sys.stderr)
    return 0


def _run_update() -> int:
    """`gc-cli --update`: собрать команду `uv tool install` и запустить
    отвязанное обновление, ждущее завершения текущего процесса.

    CLI короткоживущий — без авто-рестарта (``restart_cmd=None``); по
    завершении пользователь сам запускает gc-cli/gc-ua заново.
    """
    src = _updater_source_or_hint()
    if src is not None:
        config_dict = load_config(arg_parser.run_args.config)
        command = updater_runner.build_install_command(
            src, updater_check.extra_args(config_dict)
        )
        updater_runner.spawn_detached_update(
            command, parent_pid=os.getpid(), restart_cmd=None
        )
        print(
            f"[gc] установка запущена {updater_runner.install_location_phrase()} — "
            "по завершении запустите gc-cli/gc-ua заново",
            file=sys.stderr,
        )
    return 0


async def _run_init_agents_md() -> int:
    """`gc-cli --init`: агент сам сканирует репозиторий и пишет AGENTS.md.

    Корутина, потому что зовётся из-под ``asyncio.run(arun_cli())``, а
    синхронного входа у ядра нет вовсе.
    """
    from gc_core.instructions.init_agents_md import build_init_prompt
    from gc_core.security.confirm_port import confirm_available

    config_dict = load_config(arg_parser.run_args.config)
    auth_connector = build_auth_connector(config_dict)
    # Асинхронная сборка: мы уже внутри работающего loop. Синхронный
    # create_run_context морозил бы его на всё MCP-дискавери — замер на
    # зависшем сервере с дедлайном 2 с: 2.01 с заморозки и 9 тиков хартбита
    # против 0.01 с и 365 тиков у асинхронного двойника.
    ctx = await acreate_run_context(config_dict, auth_connector=auth_connector)
    # Тот же порт, что и в `arun_cli`. Без него вопрос агента в этом режиме шёл
    # старым путём — `input()` в потоке пула: Ctrl+C обрывал прогон, а выход
    # ждал строку в stdin. Режим `--init` как раз и спрашивает человека.
    from gc_cli.ask_human_port import make_cli_ask_human_port

    ctx.ask_human = make_cli_ask_human_port()

    from gc_core.utils.async_http import aclose_clients

    prompt = build_init_prompt(cwd=os.getcwd(), can_ask=confirm_available(ctx))
    try:
        await AgentExecutor(ctx).arun([prompt])  # агент пишет AGENTS.md сам
    finally:
        # Тот же `finally`, что и у `arun_cli`: свой `asyncio.run` — свой пул
        # клиентов, и закрыть его больше некому. Без этого `--init` уходил с
        # открытым сокетом и «Event loop is closed» на закрытии loop.
        await aclose_clients()
    print("[gc] --init: генерация AGENTS.md завершена", file=sys.stderr)
    return 0


async def _run_service_command() -> int | None:
    """Сервисные флаги (`--init-config`, `--sync-config`, …): код выхода или None.

    Вынесено из `arun_cli` не ради красоты: шесть сторожевых ветвей подряд и
    давали ту самую когнитивную сложность, на которую ругался Sonar S3776.
    """
    run_args = arg_parser.run_args
    if getattr(run_args, "init_config", False):
        return _run_init()
    if getattr(run_args, "sync_config", False):
        return _run_sync_config()
    if getattr(run_args, "reset_config", False):
        return _run_reset_config()
    if getattr(run_args, "check_update", False):
        return _run_check_update()
    if getattr(run_args, "update", False):
        return _run_update()
    if getattr(run_args, "init", False):
        return await _run_init_agents_md()
    return None


def _load_structured_format() -> tuple[str | None, int | None]:
    """Текст формы и код раннего выхода.

    Второе значение не ``None`` — файл формы не годится, агент не стартует.
    """
    format_path = getattr(
        arg_parser.run_args, "structured_output_format", None
    )
    if format_path is None:
        return None, None
    try:
        return read_format_description(format_path), None
    except StructuredOutputError as exc:
        print(f"[gc] {exc}", file=sys.stderr)
        return None, 1


def _print_agent_response(
    response: Any, config_dict: dict, ctx: RunContext
) -> None:
    formatted = format_json_for_display(response)
    if human_console_enabled(config_dict) and formatted:
        print(formatted)
    if formatted:
        ctx.logger.info(
            "final_answer",
            extra={"event": "final_answer", "answer": formatted},
        )


async def _emit_structured_if_requested(
    *,
    format_text: str | None,
    response: Any,
    config_dict: dict,
    ctx: RunContext,
    executor: AgentExecutor,
) -> int | None:
    if format_text is None:
        return None
    return await aemit_structured_result(
        response=response,
        config_dict=config_dict,
        ctx=ctx,
        state=executor.get_state(),
        terminal_outcome=executor.last_terminal_outcome,
        format_text=format_text,
    )


def _unconfigured_setup_exit(
    config_arg: str | None,
    config_dict: dict,
    *,
    just_created: bool,
    format_text: str | None,
) -> int | None:
    """Код выхода, если дефолтный конфиг ещё не настроен.

    ``None`` — конфиг указан явно или уже заполнен, агент стартует.
    С ``--structured-output-format`` код 1: пустой stdout и 0 скрипт
    принял бы за ответ. Без флага код 0, подсказка остаётся в stderr.
    Вынесено из ``arun_cli``: вложенная ветка поднимала S3776 выше 15.
    """
    if config_arg is not None or not onboarding.is_unconfigured(config_dict):
        return None
    print(
        onboarding.setup_guide(
            str(user_config_path()), config_dict, just_created=just_created
        ),
        file=sys.stderr,
    )
    if format_text is not None:
        return 1
    return 0


async def arun_cli() -> int:
    """
    Входная точка CLI: парсинг аргументов, загрузка конфига,
    создание RunContext и запуск агента.

    Возвращает код выхода (0). Финальный ответ агента печатается в stdout
    при ``console_trace: true``; сырое значение доступно через ``cli.run()``.
    С ``--structured-output-format`` перефраз всегда попадает в stdout.
    Если stdout уже поток jsonl, это событие ``final_answer``, без сырой
    строки. Иначе печатается сам текст.
    """
    _ensure_project_gc_dir()
    _ensure_user_skills_dir()

    service_exit = await _run_service_command()
    if service_exit is not None:
        return service_exit

    format_text, format_exit = _load_structured_format()
    if format_exit is not None:
        return format_exit

    config_arg = arg_parser.run_args.config
    profile_arg = getattr(arg_parser.run_args, "profile", None)
    just_created = _bootstrap_user_config() if config_arg is None else False
    try:
        config_dict, _profile_meta = load_config_ex(
            config_arg, profile_override=profile_arg
        )
    except ValueError as exc:
        # Нерезолвимый конфиг — объяснение в stderr и выход, а не голый traceback
        # из глубины загрузки (см. onboarding.explain_broken_config). Профиль —
        # тот же, что грузили: по указателю из файла объяснение описывало бы
        # чужой стек.
        print(
            onboarding.explain_broken_config(
                resolve_config_path(config_arg),
                exc,
                profile_override=profile_arg,
            ),
            file=sys.stderr,
        )
        return 1

    # Единая точка применения CLI-override поверх config.yml.
    apply_cli_overrides(
        config_dict,
        extra_overrides=getattr(arg_parser, "extra_config_overrides", None),
        write_guard_enabled=arg_parser.run_args.write_guard,
        no_write_guard=arg_parser.run_args.no_write_guard,
        command_moderation_mode=arg_parser.run_args.command_moderation,
        read_guard_enabled=getattr(arg_parser.run_args, "read_guard", None),
        no_read_guard=getattr(arg_parser.run_args, "no_read_guard", False),
        read_guard_mode=getattr(arg_parser.run_args, "read_guard_mode", None),
        agentsmd_override=getattr(
            arg_parser.run_args, "agentsmd_override", False
        ),
        trace_id=getattr(arg_parser.run_args, "trace_id", None),
    )
    # Легаси-форс (спека §2): --model/--completions_url поверх непустого реестра
    # отключает резолв уровней/алиасов на этот запуск, профиль-слой остаётся в силе.
    force_legacy = cli_forces_legacy_model(config_dict)
    if force_legacy:
        config_dict.setdefault("compat_notices", []).append(
            "--model: уровни отключены на этот запуск (легаси-форс)"
        )
    from gc_core import upgrade_gate

    _upgrade_info = upgrade_gate.detect_upgrade()

    # gc-cli больше НЕ запускает визард автоматически — только по --init-config.
    # При неполном конфиге на дефолтном пути — пассивный текстовый хинт и выход.
    setup_exit = _unconfigured_setup_exit(
        config_arg,
        config_dict,
        just_created=just_created,
        format_text=format_text,
    )
    if setup_exit is not None:
        return setup_exit

    thread_id = arg_parser.run_args.thread_id
    start_new_thread = arg_parser.run_args.new
    cli_images = await _aprepare_cli_images(
        config_dict,
        getattr(arg_parser.run_args, "images", None) or [],
        thread_id=thread_id,
        start_new_thread=start_new_thread,
    )
    if cli_images is None:
        return 1
    image_store, images, first_image_n = cli_images

    # Стамп — только на полноценном запуске: хинт-путь выше не должен «сжигать»
    # notice «агент обновлён» (ревью Task 4).
    upgrade_gate.mark_current_run()
    forced_skill_id = await _resolve_forced_skill(
        arg_parser.run_args.skill, config_dict
    )

    auth_connector = build_auth_connector(config_dict)
    auto_mcp = getattr(arg_parser.run_args, "auto_mcp", False)
    if auto_mcp:
        # У gc-cli автозапуск opt-in: выключается тем, что `-a` не передан.
        # Флага `--no-auto-mcp` здесь нет — он живёт только у gc-ua.
        ensure_mcp_daemons(
            config_dict,
            disable_hint="отключить автозапуск: запустите без `-a`",
        )
    # Асинхронная сборка: мы внутри asyncio.run(arun_cli()), и синхронный
    # create_run_context морозил бы loop на всё MCP-дискавери (замер: 2.01 с
    # против 0.01 с). Сервер и SDK давно ходят через acreate_run_context.
    ctx = await acreate_run_context(
        config_dict, auth_connector=auth_connector, force_legacy=force_legacy
    )
    # Порт «спросить человека». Без него ядро читает stdin через `to_thread`:
    # поток из пула, который `asyncio.run` ждёт на выходе, и Ctrl+C во время
    # вопроса отменяет прогон целиком вместо штатного «отвечать не стали».
    from gc_cli.ask_human_port import make_cli_ask_human_port

    ctx.ask_human = make_cli_ask_human_port()

    # Стартовые уведомления → stderr (не-блокирующие; онбординг остаётся выше).
    from gc_core.startup_notices import (
        collect_startup_notices,
        render_notices_for_cli,
    )
    from gc_core.tools.registry import all_builtin_tool_names

    _notices = collect_startup_notices(
        config_dict,
        all_builtin_tool_names(config_dict),
        getattr(ctx, "mcp_status", None),
        is_tui=False,
        known_tool_names=getattr(ctx, "registered_tool_names", None),
        deprecated_keys_used=config_dict.get("deprecated_keys_used"),
        upgrade_info=_upgrade_info,
        compat_notices=config_dict.get("compat_notices"),
    )
    if _notices:
        print(render_notices_for_cli(_notices), file=sys.stderr)

    user_parts, attachments = _cli_user_parts(
        arg_parser.run_args.query,
        images,
        first_n=first_image_n,
        wrap_technical=arg_parser.run_args.wrap_technical,
        forced_skill_id=forced_skill_id,
    )

    verbose = arg_parser.run_args.verbose

    executor = AgentExecutor(ctx)
    structured_exit: int | None = None
    # Учёт закрывается один раз, после всего, что ещё прибавится к прогону.
    # Сейчас это перефраз. Следующий такой шаг встаёт перед той же точкой.
    close_accounting_after = format_text is not None
    ran = False
    try:
        response = await executor.arun(
            user_parts,
            thread_id=thread_id,
            start_new_thread=start_new_thread,
            verbose=verbose,
            attachments=attachments,
            account_run=not close_accounting_after,
        )
        ran = True
        structured_exit = await _emit_structured_if_requested(
            format_text=format_text,
            response=response,
            config_dict=config_dict,
            ctx=ctx,
            executor=executor,
        )
    finally:
        # Запись меты может бросить исключение. Если она стоит первой
        # строкой finally, клиенты и демоны `-a` этого запуска не гасятся.
        # Учёт — в своём try, уборка — во внутреннем finally.
        try:
            if ran and close_accounting_after:
                await executor.aclose_run_accounting()
        finally:
            # Пул httpx привязан к этому loop — закрыть его надо до его
            # остановки, иначе на выходе получим «Event loop is closed»
            # из деструктора.
            from gc_core.utils.async_http import aclose_clients

            await aclose_clients()
            # Карман процесса больше не нужен: с `-t` ядро уже скопировало
            # картинки в тред, без `-t` запуск кончился вместе с ними.
            image_store.clear_own_staging()
            if auto_mcp:
                shutdown_mcp_daemons()
    if structured_exit is not None:
        return structured_exit
    _print_agent_response(response, config_dict, ctx)
    return 0


def main() -> int:
    """Синхронная точка входа команды ``gc-cli``.

    Сам прогон асинхронный (``arun_cli``), поэтому здесь заводится ОДИН loop на
    процесс, и ход агента идёт на нём же.

    Это НЕ синхронный двойник: одноимённой корутины ``amain`` больше нет.
    Пара ``main``/``amain`` читалась счётчиком как случайная — точка входа
    консольной команды обязана быть синхронной, и прогон у неё ровно один.
    """
    import asyncio

    return asyncio.run(arun_cli())


if __name__ == "__main__":
    raise SystemExit(main())
