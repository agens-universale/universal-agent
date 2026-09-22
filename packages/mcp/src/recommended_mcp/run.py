"""Запускает MCP с блоком run: в config.yml.

Поддерживаемые run.type:
  - docker-compose — docker compose up [-d]
  - command — daemon-команда под супервизором (PID-файлы, Ctrl+C гасит детей)

MCP без run-блока пропускаются — они вызываются агентом по stdio on-demand.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Позволяет запускать файл напрямую: `python recommended_mcp/run.py`.
# При `python -m recommended_mcp.run` и при import из тестов — no-op.
if __package__ in (None, ""):
    # Единственная арифметика, которая тут допустима: каталог, из которого
    # виден САМ пакет рецептов (`packages/mcp/src` в рабочей копии,
    # site-packages в колесе). Без него не собрать `import recommended_mcp`,
    # то есть не позвать общий поиск корня.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    # Дальше корень ищет repo_path ПО МАРКЕРУ. Жёсткая глубина здесь уже один
    # раз сломалась: `parents[1]` указывал на корень до переезда рецептов, а
    # после переезда стал указывать на packages/mcp/src — gc_core оставался
    # ненаходимым, и прямой запуск падал на ModuleNotFoundError. Менять её
    # на parents[4] — повторять ту же ошибку. repo_path знает и дореформенную
    # раскладку с единым <repo>/src, и кладёт на путь все packages/*/src разом.
    from recommended_mcp.runtime.repo_path import ensure_repo_on_sys_path

    ensure_repo_on_sys_path()
    __package__ = "recommended_mcp"

from recommended_mcp.manifest import MCPEntry, load_manifest
from recommended_mcp.process_supervisor import RunSupervisor, stop_command_daemons
from recommended_mcp.runtime.manifest_resolve import recipe_root, resolve_install_manifest_path


class RunError(RuntimeError):
    """Невозможно запустить MCP."""


def run_entry(entry: MCPEntry, root: Path, supervisor: RunSupervisor) -> bool:
    """Запустить один MCP. True — стартовали supervised daemon в этом вызове."""
    if entry.run is None:
        print(f"[skip] {entry.name}: no run step (stdio/on-demand)")
        return False

    pkg_path = (root / entry.path).resolve()
    rtype = entry.run.type
    if rtype == "docker-compose":
        _run_docker_compose(entry, pkg_path)
        return False
    if rtype == "command":
        return supervisor.start_if_needed(entry.name, entry.run, cwd=pkg_path)
    raise RunError(
        f"mcp {entry.name!r} uses run.type={rtype!r} which is not supported "
        f"by this run.py. Update run.py to handle {rtype!r}, "
        f"or remove the run: block in config.yml."
    )


def _run_docker_compose(entry: MCPEntry, pkg_path: Path) -> None:
    compose = pkg_path / "docker-compose.yml"
    if not compose.exists():
        raise RunError(f"mcp {entry.name!r}: docker-compose.yml not found at {compose}")
    cmd = ["docker", "compose", "-f", str(compose), "up"]
    if entry.run and entry.run.detach:
        cmd.append("-d")
    print(f"[run] {entry.name}: {' '.join(cmd[-2:])}")
    subprocess.run(cmd, check=True)


def _command_daemon_names(manifest: list[MCPEntry]) -> list[str]:
    return [entry.name for entry in manifest if entry.run is not None and entry.run.type == "command"]


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run long-lived MCPs from recommended_mcp manifest",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Путь к кастомному конфигурационному файлу манифеста MCP (по умолчанию recommended_mcp/config.yml).",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Остановить daemon-команды (run.type=command) из манифеста по PID-файлам.",
    )
    args = parser.parse_args()

    root = recipe_root()
    manifest_path = resolve_install_manifest_path(args.config)
    print(f"[config] {manifest_path}")
    manifest = load_manifest(manifest_path)
    if not manifest:
        print("[info] no mcps declared in config.yml — nothing to run")
        return 0

    if args.stop:
        stopped = stop_command_daemons(_command_daemon_names(manifest))
        print(f"[done] stopped {stopped} daemon(s)")
        return 0

    supervisor = RunSupervisor()
    started_any = False
    try:
        for entry in manifest:
            if run_entry(entry, root, supervisor):
                started_any = True
        if started_any:
            print("[run] supervising started daemons — Ctrl+C to stop them")
            supervisor.wait_until_signal()
        else:
            print("[run] nothing new to supervise (already running or stdio-only)")
    finally:
        supervisor.shutdown_managed()
    print("[done] run complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
