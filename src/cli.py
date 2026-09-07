import os
import sys
from typing import Any

import arg_parser
import config_sync
import onboarding
from agent_executor import AgentExecutor
from config import (
    apply_cli_overrides,
    build_auth_connector,
    cli_forces_legacy_model,
    load_config,
    load_config_ex,
    resolve_config_path,
    user_config_path,
    _bootstrap_user_config,
)
from message_policy.user_message_policy import (
    wrap_skill_resolution_technical_instruction,
    wrap_technical_user_content,
)
from run_context import RunContext, create_run_context
from mcp_daemon_autostart import ensure_mcp_daemons, shutdown_mcp_daemons
from updater import check as updater_check
from updater import runner as updater_runner
from updater import source as updater_source
from utils.json_text import format_json_for_display
from utils.logging_module import human_console_enabled


def _ensure_project_gc_dir() -> None:
    """Создаёт <cwd>/.gc/ при старте gc-cli. Делегирует общему хелперу
    (тот же вызывает gc-ua) — см. env_vars.ensure_project_gc_dir."""
    from config_modules.env_vars import ensure_project_gc_dir
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
        from config_modules.env_vars import gc_paths
        os.makedirs(gc_paths()["data"] / "skills", exist_ok=True)
    except (PermissionError, OSError):
        pass


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


def _resolve_forced_skill(skill_name: str, config_dict: dict) -> str | None:
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
    from tools.skills.skill_arg_resolver import resolve_skill_arg, Failed
    from tools.skills.registry import get_skills_registry

    try:
        catalog = get_skills_registry(config_dict).skill_catalog()
    except Exception as exc:  # noqa: BLE001 - surface as warning, not traceback
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
        print(onboarding.explain_broken_config(str(path), exc), file=sys.stderr)
        return 1
    config_dict = loaded.config
    if loaded.message:
        # Мастер gc-cli плоский, экран не занимает — stderr человек видит.
        print(f"[gc] {loaded.message}", file=sys.stderr)
    if _interactive():
        import onboarding_wizard
        configured = onboarding_wizard.run(
            config_dict, path, review_all=True,
            io=onboarding_wizard.default_io(),   # без mcp — gc-cli не ставит MCP
            # Ступень «без профиля» значит, что конфиг собрался только со снятым
            # профилем: финал обязан снять и указатель, иначе записать починку
            # некуда. Но снять его мастер может не всегда (указатель в managed-слое),
            # и судит об этом лестница: своя редакция признака обещала бы человеку
            # деактивацию, которую писатель не сделает, — а пустой якорь в базу
            # уже уехал бы (ревью D3).
            deactivate_profile=loaded.deactivate_profile)
        # run() отдаёт False не только на трейсбеке, но и когда человек прошёл
        # мастер, а конфиг всё равно остался ненастроенным (пропущенные поля,
        # отказ записи, файл, который не удалось перечитать) — код возврата
        # обязан отражать это, а не рапортовать успех при честном stderr рядом.
        return 0 if configured else 1
    else:
        # Гайд знает ступень лестницы: на деградировавшей «настроен — можно
        # запускать» врало бы человеку, чей следующий старт падает.
        print(onboarding.setup_guide(str(path), config_dict, just_created=just_created,
                                     repair=loaded), file=sys.stderr)
    return 0


def _run_sync_config() -> int:
    """`gc-cli --sync-config`: обновить config.yml по свежему шаблону и выйти."""
    from pathlib import Path
    from config import _default_config_path
    from config_modules.env_vars import project_local_config_path

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
        lines.append(f"     устаревших убрано: {', '.join(report.removed)} (в бэкапе)")
    if report.unknown:
        lines.append(f"     неизвестных ключей: {', '.join(report.unknown)} (в бэкапе)")
    if report.reenter:
        lines.append(f"     задайте заново (формат изменился): {', '.join(report.reenter)}")
    lines.append(f"     бэкап: {report.backup_path}")
    lines.append("     личные комментарии из старого файла не переносятся — они в бэкапе.")
    print("\n".join(lines), file=sys.stderr)
    return 0


def _run_reset_config() -> int:
    """`gc-cli --reset-config`: подменить config.yml пакетным умолчанием (спека §11).

    Файлы профилей (`~/.gc/profiles/*`) и сессионные оверрайды не трогает — заменяет
    ровно тот файл, что был бы базой запуска (вместе с его указателем `profile:`:
    выбранный профиль после сброса деактивирован). Интерактивное y/N-подтверждение;
    без TTY — честный отказ (не молчаливое да и не молчаливый отказ)."""
    from pathlib import Path
    from config import _default_config_path
    from config_modules.env_vars import project_local_config_path

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
            "[gc] --reset-config требует интерактивного подтверждения (TTY) — "
            "запустите в терминале и подтвердите y/N.",
            file=sys.stderr,
        )
        return 1
    print(
        f"[gc] Сбросить {target} к пакетному умолчанию? Текущий файл уйдёт в бэкап. [y/N]: ",
        file=sys.stderr, end="",
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
        f"[gc] ✓ config.yml сброшен к пакетному умолчанию: {target}\n"
        f"     бэкап: {report.backup_path}",
        file=sys.stderr,
    )
    return 0


def _run_check_update() -> int:
    """`gc-cli --check-update`: разовая проверка обновлений и выход.

    Работает и при ``update.check: off`` в config.yml — это ручная команда,
    троттлинг авто-чека её не касается.
    """
    src = updater_source.resolved_source()
    if src is None:
        print(
            "[gc] обновление недоступно для этой установки — обновляйтесь через git (dev-установка)",
            file=sys.stderr,
        )
        return 0
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
    src = updater_source.resolved_source()
    if src is None:
        print(
            "[gc] обновление недоступно для этой установки — обновляйтесь через git (dev-установка)",
            file=sys.stderr,
        )
        return 0
    config_dict = load_config(arg_parser.run_args.config)
    command = updater_runner.build_install_command(src, updater_check.extra_args(config_dict))
    updater_runner.spawn_detached_update(command, parent_pid=os.getpid(), restart_cmd=None)
    print(
        f"[gc] установка запущена {updater_runner.install_location_phrase()} — "
        "по завершении запустите gc-cli/gc-ua заново",
        file=sys.stderr,
    )
    return 0


def _run_init_agents_md() -> int:
    """`gc-cli --init`: агент сам сканирует репозиторий и пишет AGENTS.md."""
    from instructions.init_agents_md import build_init_prompt
    from security.confirm_port import confirm_available

    config_dict = load_config(arg_parser.run_args.config)
    auth_connector = build_auth_connector(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector)

    prompt = build_init_prompt(cwd=os.getcwd(), can_ask=confirm_available(ctx))
    AgentExecutor(ctx).run([prompt])  # агент пишет AGENTS.md сам (write_to_file)
    print("[gc] --init: генерация AGENTS.md завершена", file=sys.stderr)
    return 0


def main() -> int:
    """
    Входная точка CLI: парсинг аргументов, загрузка конфига,
    создание RunContext и запуск агента.

    Возвращает код выхода (0). Финальный ответ агента печатается в stdout
    при ``console_trace: true``; сырое значение доступно через ``cli.run()``.
    """
    _ensure_project_gc_dir()
    _ensure_user_skills_dir()

    if getattr(arg_parser.run_args, "init_config", False):
        return _run_init()

    if getattr(arg_parser.run_args, "sync_config", False):
        return _run_sync_config()

    if getattr(arg_parser.run_args, "reset_config", False):
        return _run_reset_config()

    if getattr(arg_parser.run_args, "check_update", False):
        return _run_check_update()

    if getattr(arg_parser.run_args, "update", False):
        return _run_update()

    if getattr(arg_parser.run_args, "init", False):
        return _run_init_agents_md()

    config_arg = arg_parser.run_args.config
    profile_arg = getattr(arg_parser.run_args, "profile", None)
    just_created = _bootstrap_user_config() if config_arg is None else False
    try:
        config_dict, _profile_meta = load_config_ex(config_arg, profile_override=profile_arg)
    except ValueError as exc:
        # Нерезолвимый конфиг — объяснение в stderr и выход, а не голый traceback
        # из глубины загрузки (см. onboarding.explain_broken_config). Профиль —
        # тот же, что грузили: по указателю из файла объяснение описывало бы
        # чужой стек.
        print(onboarding.explain_broken_config(
            resolve_config_path(config_arg), exc, profile_override=profile_arg),
            file=sys.stderr)
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
        agentsmd_override=getattr(arg_parser.run_args, "agentsmd_override", False),
        trace_id=getattr(arg_parser.run_args, "trace_id", None),
    )
    # Легаси-форс (спека §2): --model/--completions_url поверх непустого реестра
    # отключает резолв уровней/алиасов на этот запуск, профиль-слой остаётся в силе.
    force_legacy = cli_forces_legacy_model(config_dict)
    if force_legacy:
        config_dict.setdefault("compat_notices", []).append(
            "--model: уровни отключены на этот запуск (легаси-форс)"
        )
    import upgrade_gate
    _upgrade_info = upgrade_gate.detect_upgrade()

    # gc-cli больше НЕ запускает визард автоматически — только по --init-config.
    # При неполном конфиге на дефолтном пути — пассивный текстовый хинт и выход.
    if config_arg is None and onboarding.is_unconfigured(config_dict):
        print(onboarding.setup_guide(str(user_config_path()), config_dict, just_created=just_created), file=sys.stderr)
        return 0

    # Стамп — только на полноценном запуске: хинт-путь выше не должен «сжигать»
    # notice «агент обновлён» (ревью Task 4).
    upgrade_gate.mark_current_run()
    forced_skill_id = _resolve_forced_skill(
        arg_parser.run_args.skill, config_dict
    )

    auth_connector = build_auth_connector(config_dict)
    auto_mcp = getattr(arg_parser.run_args, "auto_mcp", False)
    if auto_mcp:
        ensure_mcp_daemons(config_dict)
    ctx = create_run_context(config_dict, auth_connector=auth_connector, force_legacy=force_legacy)

    # Стартовые уведомления → stderr (не-блокирующие; онбординг остаётся выше).
    from startup_notices import collect_startup_notices, render_notices_for_cli
    from tools.registry import all_builtin_tool_names
    _notices = collect_startup_notices(
        config_dict,
        all_builtin_tool_names(config_dict),
        getattr(ctx, "mcp_status", None),
        is_tui=False,
        deprecated_keys_used=config_dict.get("deprecated_keys_used"),
        upgrade_info=_upgrade_info,
        compat_notices=config_dict.get("compat_notices"),
    )
    if _notices:
        print(render_notices_for_cli(_notices), file=sys.stderr)

    user_parts: list[str] = [arg_parser.run_args.query]
    if arg_parser.run_args.wrap_technical:
        user_parts[0] = wrap_technical_user_content(user_parts[0])
    if forced_skill_id:
        user_parts.append(
            wrap_skill_resolution_technical_instruction(forced_skill_id)
        )

    verbose = arg_parser.run_args.verbose
    thread_id = arg_parser.run_args.thread_id
    start_new_thread = arg_parser.run_args.new

    executor = AgentExecutor(ctx)
    try:
        response = executor.run(
            user_parts,
            thread_id=thread_id,
            start_new_thread=start_new_thread,
            verbose=verbose,
        )
    finally:
        if auto_mcp:
            shutdown_mcp_daemons()
    formatted = format_json_for_display(response)
    if human_console_enabled(config_dict):
        if formatted:
            print(formatted)
    if formatted:
        ctx.logger.info(
            "final_answer",
            extra={"event": "final_answer", "answer": formatted},
        )
    return 0


if __name__ == "__main__":
    # -q "создай подпапку test_project. в ней напиши на python модуль вычисления чисел фиббоначи. там же в отдельном файле напиши тесты. как прогонишь все тесты, запусти посчитай первые 1000е числео фиббоначи и сложи результат в json файл там же. Пиши быстрый код, чтобы кешировало расчеты и долго не считало. Скажи мне чему будет равно 1000-е числов фиббоначи, умноженное на 3."
    main()
