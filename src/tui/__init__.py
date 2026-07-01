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
        "--init",
        action="store_true",
        help="Создать ~/.gc/config.yml из шаблона и выйти (TUI не запускается)",
    )
    parser.add_argument(
        "-a",
        "--auto-mcp",
        action="store_true",
        help=(
            "Поднять HTTP/SSE MCP-daemon'ы из recommended_mcp/ перед запуском "
            "(если каталог есть в cwd); по завершении gc-ua гасятся только "
            "процессы, запущенные этим вызовом"
        ),
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

    if getattr(args, "init", False):
        path = user_config_path()
        if _bootstrap_user_config():
            print(onboarding.setup_guide(str(path), load_config(None)), file=sys.stderr)
        else:
            print(f"[gc] конфиг уже есть: {path} — не меняю", file=sys.stderr)
        return 0

    config_dict = load_config(args.config)
    apply_cli_overrides(config_dict)

    # Онбординг: дефолтный путь и конфиг ещё не настроен → гайд ДО входа в
    # alt-screen (иначе TUI проглотит stderr) и выходим, бэкенд не дёргаем.
    if args.config is None and onboarding.is_unconfigured(config_dict):
        print(
            onboarding.setup_guide(str(user_config_path()), config_dict),
            file=sys.stderr,
        )
        return 0

    config_dict["console_trace"] = False  # TUI owns alt-screen output.

    from thread_store import resolve_threads_dir
    threads_dir = resolve_threads_dir(config_dict)

    from mcp_daemon_autostart import ensure_mcp_daemons, shutdown_mcp_daemons

    auto_mcp = args.auto_mcp
    if auto_mcp:
        ensure_mcp_daemons(config_dict)

    app = _app_mod.TuiApp(threads_dir=threads_dir, system="cli", config=config_dict)
    app._config_path = resolve_config_path(args.config)
    app._config_project_local = _project_local_config_path()  # str | None
    try:
        app.run()
    finally:
        if auto_mcp:
            shutdown_mcp_daemons()
    return 0
