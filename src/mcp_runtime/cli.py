"""gc-mcp — установка/подключение/снос MCP по имени из курируемого манифеста."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence


def _load_manifest_entries(config: str | None = None):
    """(entries, recipe_root). Резолвит манифест (config=None → дефолтная цепочка)."""
    from mcp_runtime.repo_path import ensure_repo_on_sys_path
    ensure_repo_on_sys_path()
    from mcp_runtime.manifest_resolve import resolve_install_manifest_path, recipe_root
    from recommended_mcp.manifest import load_manifest
    path = resolve_install_manifest_path(config)
    return load_manifest(path), recipe_root()


def _agent_dep_dir():
    from config_modules.env_vars import gc_paths
    return gc_paths()["data"]


def _install_one(entry, root: Path, agent_dep_dir, editable: bool = False) -> bool:
    """Поставить один рецепт в MCP-venv. True если успех."""
    from recommended_mcp.install import run_install_entries
    from mcp_runtime.venv import ensure_mcp_venv, pip_install_target
    ensure_mcp_venv()
    with pip_install_target():
        results = run_install_entries([entry], root, agent_dep_dir, editable=editable)
    return any(s == "installed" for _, s, _ in results)


def _wire_one(entry, root: Path, agent_dep_dir) -> None:
    from mcp_runtime import wiring
    from recommended_mcp.install import _node_install_paths
    from config_modules.env_vars import gc_paths
    npx = str(_node_install_paths(agent_dep_dir)["npx"])
    cache = str(gc_paths()["cache"])
    recipe_dir = (root / entry.path).resolve()
    try:
        leftover = wiring.wire_snippet(entry.name, recipe_dir, npx=npx, cache=cache)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[warn] {entry.name}: wire пропущен — {exc}")
        return
    if leftover:
        print(f"[warn] {entry.name}: незаполненные плейсхолдеры {leftover}")
    print(f"[wired] {entry.name} → config.local.yml")


def _uninstall_one(entry, root: Path) -> None:
    from recommended_mcp.install import uninstall_entry
    uninstall_entry(entry, root)


def _cmd_install(name: Optional[str], config: Optional[str] = None, editable: bool = False) -> int:
    entries, root = _load_manifest_entries(config)
    dep = _agent_dep_dir()
    from recommended_mcp.install import find_entry
    if name:
        entry = find_entry(entries, name)
        if entry is None:
            print(f"[error] MCP '{name}' не найден в манифесте. Доступны: "
                  f"{', '.join(e.name for e in entries)}", file=sys.stderr)
            return 2
        if _install_one(entry, root, dep, editable):
            _wire_one(entry, root, dep)
            print(f"[done] {name} установлен и подключён")
        else:
            print(f"[warn] {name} не установлен — не подключаю", file=sys.stderr)
        return 0
    installed_names: list[str] = []
    failed_names: list[str] = []
    for entry in entries:
        if _install_one(entry, root, dep, editable):
            _wire_one(entry, root, dep)
            installed_names.append(entry.name)
        else:
            failed_names.append(entry.name)
    print(f"[summary] установлено+подключено: {', '.join(installed_names) or '—'}")
    if failed_names:
        print(f"[summary] не установлено: {', '.join(failed_names)}")
    print("[done] install complete")
    return 0


def _cmd_list() -> int:
    entries, root = _load_manifest_entries()
    from mcp_runtime import wiring
    from mcp_runtime.venv import query_installed_version
    from recommended_mcp.install import _dist_name
    wired = wiring.wired_servers()
    print(f"{'NAME':22} {'TRANSPORT':14} {'INSTALLED':10} WIRED")
    for e in entries:
        dist = _dist_name(e, root) if e.install.type in ("pypi", "source", "wheel") else None
        installed = "yes" if (dist and query_installed_version(dist)) else "—"
        srv = wired.get(e.name) or {}
        transport = srv.get("transport", "—")
        print(f"{e.name:22} {transport:14} {installed:10} "
              f"{'yes' if e.name in wired else '—'}")
    return 0


def _cmd_remove(name: str, keep_package: bool) -> int:
    from mcp_runtime import wiring
    entries, root = _load_manifest_entries()
    from recommended_mcp.install import find_entry
    if wiring.unwire(name):
        print(f"[unwired] {name}")
    else:
        print(f"[info] {name} не был подключён")
    if not keep_package:
        entry = find_entry(entries, name)
        if entry is not None:
            _uninstall_one(entry, root)
        else:
            print(f"[warn] {name} нет в манифесте — деинсталл пропущен")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="gc-mcp")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_inst = sub.add_parser("install", help="поставить MCP по имени (или все) + авто-wire")
    p_inst.add_argument("name", nargs="?", default=None)
    p_inst.add_argument("--config", default=None, metavar="PATH",
                        help="кастомный манифест MCP (по умолчанию — дефолтная цепочка)")
    p_inst.add_argument("-e", "--editable", action="store_true",
                        help="source-MCP в editable-режиме (pip install -e)")
    sub.add_parser("list", help="доступные / установленные / подключённые MCP")
    p_rm = sub.add_parser("remove", help="отключить + деинсталл MCP")
    p_rm.add_argument("name")
    p_rm.add_argument("--keep-package", action="store_true",
                      help="только un-wire, пакет в venv оставить")
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])

    if args.cmd == "install":
        return _cmd_install(args.name, args.config, args.editable)
    if args.cmd == "list":
        return _cmd_list()
    if args.cmd == "remove":
        return _cmd_remove(args.name, args.keep_package)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
