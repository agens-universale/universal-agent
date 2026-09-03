"""gc-ua — entrypoint.

Тонкий парсинг startup-аргументов (--config, --task-root) + сборка
стека: load_config → threads_dir → TuiApp.
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import Optional, Sequence


_MISSING_TEXTUAL_MSG = (
    "TUI mode requires 'textual'. Reinstall: uv tool install -e . --force"
)


def _interactive() -> bool:
    """TTY на входе и выходе → можно запускать интерактивный визард."""
    import sys as _sys
    return _sys.stdin.isatty() and _sys.stdout.isatty()


def _force_truecolor_if_supported() -> None:
    """Подсказать Textual использовать 24-bit на терминалах, где truecolor точно
    есть (Windows Terminal / VS Code / COLORTERM). Иначе авто-детект может выбрать
    256 цветов, и поколоночный градиент логотипа квантуется в видимые «ступеньки»
    (наблюдалось на Windows; на macOS truecolor определяется сам). `setdefault` не
    перетирает явный пользовательский TEXTUAL_COLOR_SYSTEM."""
    if (
        os.environ.get("WT_SESSION")
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or os.environ.get("COLORTERM") in ("truecolor", "24bit")
    ):
        os.environ.setdefault("TEXTUAL_COLOR_SYSTEM", "truecolor")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entrypoint для `gc-ua = "tui:main"`."""
    _force_truecolor_if_supported()
    try:
        # importlib.import_module goes through sys.modules, not the package
        # attribute cache, so evicting tui.app in tests forces a real re-import.
        _app_mod = importlib.import_module("tui.app")
    except ImportError:
        print(_MISSING_TEXTUAL_MSG, file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(prog="gc-ua", add_help=False)
    parser.add_argument("--config", default=None)
    parser.add_argument("--task-root", default=None)
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
        "--init-config",
        action="store_true",
        help="Создать ~/.gc/config.yml из шаблона и выйти (TUI не запускается)",
    )
    # По умолчанию поднимаем HTTP/SSE-daemon'ы для MCP из конфига
    # (пересечение с манифестом recommended_mcp). Отключить: --no-auto-mcp.
    # -a/--auto-mcp оставлены для совместимости (no-op при default=True).
    parser.set_defaults(auto_mcp=True)
    parser.add_argument(
        "-a",
        "--auto-mcp",
        action="store_true",
        dest="auto_mcp",
        help=(
            "Поднять HTTP/SSE MCP-daemon'ы для серверов из mcp-servers "
            "(по умолчанию включено; пересечение с манифестом recommended_mcp); "
            "по завершении гасятся только процессы этого запуска"
        ),
    )
    parser.add_argument(
        "--no-auto-mcp",
        action="store_false",
        dest="auto_mcp",
        help="Не поднимать HTTP/SSE MCP-daemon'ы при старте",
    )
    args, _unknown = parser.parse_known_args(
        list(argv) if argv is not None else sys.argv[1:]
    )

    # Паритет с gc-cli: создать <cwd>/.gc/ (project-local слой: config overlay,
    # project skills). Тихо игнорит отсутствие прав на запись в cwd.
    from config_modules.env_vars import ensure_project_gc_dir
    ensure_project_gc_dir()

    from config import (
        load_config_ex,
        apply_cli_overrides,
        resolve_config_path,
        _project_local_config_path,
        user_config_path,
        _bootstrap_user_config,
    )
    import onboarding

    onb_write_path = user_config_path()
    onboarding_mode = None

    if getattr(args, "init_config", False):
        just_created = _bootstrap_user_config()
        # «Чинилка» обязана работать именно тогда, когда конфиг сломан: лестница
        # ступеней и текст деградации — общие с gc-cli (onboarding, один дом).
        # Целится она в ПОЛЬЗОВАТЕЛЬСКИЙ файл (`onb_write_path`) — тот же, в
        # который ниже пишет мастер, поэтому --config/--profile здесь не в счёт:
        # читать один файл, а писать в другой значило бы показать человеку поля
        # не того конфига, который он чинит.
        loaded = onboarding.load_config_for_repair()
        config_dict, profile_meta = loaded.config, loaded.meta
        if not _interactive():
            # TUI не стартует — stderr доезжает до человека и остаётся на экране.
            if loaded.message:
                print(f"[gc] {loaded.message}", file=sys.stderr)
            print(onboarding.setup_guide(str(onb_write_path), config_dict, program="gc-ua", just_created=just_created), file=sys.stderr)
            return 0
        if loaded.message:
            # А здесь stderr не живёт: через мгновение Textual занимает экран
            # альтернативным буфером и стирает строку — человек не видит НИЧЕГО
            # и не понимает, почему поля мастера ведут себя странно (живой смок
            # это подтвердил). Тем же каналом, что и остальные рантайм-события:
            # карточка + /notices.
            from runtime_notices import push_runtime_notice
            from startup_notices import Notice
            push_runtime_notice(Notice(
                "warning", "config_load_degraded", loaded.message, ("config.yml",)))
        onboarding_mode = "review"
    else:
        just_created = _bootstrap_user_config() if args.config is None else False
        try:
            config_dict, profile_meta = load_config_ex(args.config, profile_override=args.profile)
        except ValueError as exc:
            # Нерезолвимый конфиг — объяснение в stderr и выход, а не голый
            # traceback: падение здесь случается ДО создания TuiApp, так что
            # собственный crash-репорт Textual не включится (см. explain_broken_config).
            print(onboarding.explain_broken_config(
                resolve_config_path(args.config), exc, program="gc-ua"), file=sys.stderr)
            return 1
        apply_cli_overrides(config_dict)

        if args.config is None and onboarding.is_unconfigured(config_dict):
            if not _interactive():
                print(onboarding.setup_guide(str(onb_write_path), config_dict, program="gc-ua", just_created=just_created), file=sys.stderr)
                return 0
            onboarding_mode = "auto"

    # Поля, вычисляемые рантаймом TUI (в файле конфига их нет) — владелец
    # tui/runtime_config; тот же модуль переносит их через hot-reload настроек.
    from tui.runtime_config import apply_runtime_fields
    apply_runtime_fields(config_dict)

    from thread_store import resolve_threads_dir
    threads_dir = resolve_threads_dir(config_dict)

    from mcp_daemon_autostart import ensure_mcp_daemons, shutdown_mcp_daemons

    auto_mcp = args.auto_mcp
    if auto_mcp:
        ensure_mcp_daemons(config_dict)

    voice_expected = False
    _release_voice = None
    try:
        from voice_runtime.autostart import release_voice_service, url_is_ours
        from voice_runtime.state import load_state as _voice_state

        _release_voice = release_voice_service
        _vs = _voice_state()
        voice_expected = _vs is not None and url_is_ours(config_dict, _vs.port)
    except Exception:
        voice_expected = False

    app = _app_mod.TuiApp(threads_dir=threads_dir, system="cli", config=config_dict)
    app.set_config_path(onb_write_path if onboarding_mode else resolve_config_path(args.config))
    app._config_project_local = _project_local_config_path()  # str | None
    # Подъём (`ensure_voice_service`: spawn + ожидание готовности, до 90 с на холодном
    # старте) идёт ЦЕЛИКОМ фоном: TuiApp запускает его после первого кадра
    # (on_mount → _start_voice_autostart), а main() только считает флаг ожидания —
    # спавна здесь нет вовсе. Раньше вызов стоял тут СИНХРОННО и держал пустой терминал
    # перед `app.run()` весь бюджет ожидания (было 15 с, стало 90 — живая приёмка
    # поймала «программа зависла»).
    app._voice_autostart_pending = voice_expected
    # Спека §3.5: холодный старт с проектным профилем, выбранным самим config.yml
    # (не сессионным переключением) — тот же notice, что и сессионный путь
    # (`TuiApp._profile_remerge`), но здесь ProfileMeta есть только на этом
    # единственном старте загрузки, до создания app/треда.
    app._maybe_notice_profile_activation(profile_meta.active_name or "", profile_meta, config_dict)
    app._onboarding_mode = onboarding_mode
    app._onboarding_config_path = onb_write_path
    import upgrade_gate
    app._upgrade_info = upgrade_gate.detect_upgrade()
    if app._upgrade_info is None:
        # Нет апгрейда в этом запуске — стампим сразу (база для будущего
        # detect_upgrade: первый запуск / повтор той же версии).
        upgrade_gate.mark_current_run()
    # Если апгрейд ЕСТЬ — стамп откладывается до закрытия PostUpdateScreen
    # (спека D, Task 10): иначе крэш до показа экрана «съедает» уведомление
    # об апгрейде на следующем запуске.
    try:
        app.run()
    finally:
        # Осознанно две РАЗНЫЕ модели гашения демонов: voice — light-switch
        # (lease-файлы, гасит последний живой клиент, безопасно при N TUI);
        # MCP — per-process («гашу только то, что сам поднял»). Не унифицировано:
        # lease-модель для MCP — кандидат в апстрим (develop).
        # release всегда (не только при voice_ok): прибирает «опоздавший» сервис,
        # поднявшийся после таймаута, и мёртвые lease прошлых запусков. Безусловность
        # безопасна ровно потому, что гасит он только ПОМЕЧЕННЫЙ агентский сервис
        # (`leases.is_agent_owned`): форграундный `gc-voice serve` человека переживёт
        # выход из TUI, который голоса и не касался.
        if _release_voice is not None:
            try:
                _release_voice()
            except Exception:
                pass
        if auto_mcp:
            shutdown_mcp_daemons()
    return 0
