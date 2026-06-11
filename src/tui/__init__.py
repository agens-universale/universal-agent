"""gc-ua — entrypoint.

Тонкий парсинг startup-аргументов (--config, --task-root) + сборка
стека: load_config → threads_dir → TuiApp.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from typing import Optional, Sequence


_MISSING_TEXTUAL_MSG = (
    "TUI mode requires 'textual'. Install: pip install gc-cli-pub[tui]"
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entrypoint для `gc-ua = "tui:main"`."""
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
    args, _unknown = parser.parse_known_args(
        list(argv) if argv is not None else sys.argv[1:]
    )

    from config import (
        load_config,
        apply_cli_overrides,
        resolve_config_path,
        _project_local_config_path,
    )
    config_dict = load_config(args.config)
    apply_cli_overrides(config_dict)
    config_dict["console_trace"] = False  # TUI owns alt-screen output.

    from thread_store import resolve_threads_dir
    threads_dir = resolve_threads_dir(config_dict)

    app = _app_mod.TuiApp(threads_dir=threads_dir, system="cli", config=config_dict)
    app._config_path = resolve_config_path(args.config)
    app._config_project_local = _project_local_config_path()  # str | None
    app.run()
    return 0
