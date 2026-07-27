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
        load_config,
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
        config_dict = load_config(None)
        if not _interactive():
            print(onboarding.setup_guide(str(onb_write_path), config_dict, program="gc-ua", just_created=just_created), file=sys.stderr)
            return 0
        onboarding_mode = "review"
    else:
        just_created = _bootstrap_user_config() if args.config is None else False
        config_dict = load_config(args.config)
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

    app = _app_mod.TuiApp(threads_dir=threads_dir, system="cli", config=config_dict)
    app.set_config_path(onb_write_path if onboarding_mode else resolve_config_path(args.config))
    app._config_project_local = _project_local_config_path()  # str | None
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
        if auto_mcp:
            shutdown_mcp_daemons()
    return 0
