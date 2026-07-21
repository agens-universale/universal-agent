import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from execution_workspace import build_execution_workspace, host_os_label
from utils.directory_write_access import directory_allows_probe_write

cut_first = 100
cut_second = 120  # D4: was 30, raised to cover ~50-53 children in typical src/

_TRUNCATE_MARKER = "(truncated {shown}/{total})"  # D5
_FINAL_HINT = "[projectDirListing truncated — read the needed directory separately]"  # D5


def _sort_key(name: str, is_dir: bool) -> tuple[int, str]:
    """
    stable ordering — dirs first, then casefold alpha.
    Returns (0, name.casefold()) for dirs, (1, name.casefold()) for files.
    """
    return (0 if is_dir else 1, name.casefold())


def _slice_level(
    entries: list[tuple[str, bool]],
    limit: int,
) -> tuple[list[tuple[str, bool]], bool, int, int]:
    """
    stable sort, slice by shown count, record truncation.

    Returns (shown, was_truncated, shown_count, total_count).
    """
    total = len(entries)
    sorted_entries = sorted(entries, key=lambda e: _sort_key(e[0], e[1]))
    shown = sorted_entries[:limit]
    return shown, len(shown) < total, len(shown), total

stop_folders = {
        '.git', '.hg', '.svn', '.osc',
        '__pycache__', 'venv', '.venv', '.env',
        'node_modules', 'bower_components',
        'dist', 'build', 'out', 'target', 'bin', 'obj',
        '.vscode', '.idea', '.fleet',
        '.mypy_cache', '.pytest_cache', '.coverage',
        '.gradle', '.classpath', '.project', '.settings',
        'packages', '.nuget', 'TestResults',
        'coverage', '.nyc_output',
        'logs', 'tmp', 'temp', 'dump.rdb',
        '.dockerignore', '.env.local', '.env.*',
        '.github', '.gitlab', '.circleci', '.travis',
        'Thumbs.db', 'Desktop.ini', '.DS_Store',
        '$RECYCLE.BIN', 'System Volume Information',
        '.Trash-*', '.local/share/Trash',
        'patchwork', '.verdaccio', '.pnp', '.pnpify',
        'Cargo.lock', 'target', 'target_debug', 'target_release',
        'vendor'
    }


def get_listing(dir_path: str) -> str:
    def _listdir(p: str) -> list[tuple[str, bool]]:
        return [(f, os.path.isdir(os.path.join(p, f))) for f in os.listdir(p)]

    def _join(base: str, name: str) -> str:
        return os.path.join(base, name)

    return _build_listing(_listdir, _join, dir_path)


def get_listing_from_backend(backend: Any, dir_path: str) -> str:
    def _listdir(p: str) -> list[tuple[str, bool]]:
        return list(backend.listdir(p))

    def _join(base: str, name: str) -> str:
        return backend.join(base, name)

    return _build_listing(_listdir, _join, dir_path)


def _build_listing(
    listdir_fn,
    join_fn,
    dir_path: str,
) -> str:
    """
    shared two-level listing builder for both FS and backend.

    - Stable sort: dirs-first, casefold alpha (D2).
    - Limit counts shown names after stop_folders filter (D3).
    - Level marker: ``...(truncated N/total)`` on any truncated level (D5).
    - Final hint: one tool-agnostic line at end of listing,
      present ONLY when at least one level was truncated (D5).
    """
    raw_entries = listdir_fn(dir_path)

    filtered: list[tuple[str, bool]] = [
        (n, d) for n, d in raw_entries if n not in stop_folders
    ]

    shown, root_trunc, root_shown, root_total = _slice_level(filtered, cut_first)
    any_trunc = root_trunc

    res: List[tuple[str, List[str]]] = []
    for name, is_dir in shown:
        if is_dir:
            sub_path = join_fn(dir_path, name)
            raw_sub = listdir_fn(sub_path)
            filtered_sub: list[tuple[str, bool]] = [
                (n, d) for n, d in raw_sub if n not in stop_folders
            ]
            shown_sub, sub_trunc, sub_shown, sub_total = _slice_level(filtered_sub, cut_second)
            if sub_trunc:
                any_trunc = True
            sub_items = [n + "/" if d else n for n, d in shown_sub]
            if sub_trunc:
                sub_items.append(
                    "..." + _TRUNCATE_MARKER.format(shown=sub_shown, total=sub_total)
                )
            res.append((name + "/", sub_items))
        else:
            res.append((name, []))

    if root_trunc:
        res.append((
            "..." + _TRUNCATE_MARKER.format(shown=root_shown, total=root_total),
            [],
        ))

    tpl = ""
    for name, sub_items in res:
        tpl += "\n" + name
        for sub in sub_items:
            tpl += "\n    " + sub

    if any_trunc:
        tpl += "\n" + _FINAL_HINT

    return tpl.strip()


def _dir_writable_probe(path: str) -> bool:
    return directory_allows_probe_write(path)


def _dedupe_ordered_abs_paths(paths: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for raw in paths:
        candidate = os.path.abspath(os.path.expanduser(os.path.normpath(str(raw))))
        if candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _resolve_workspace_dir(config_dict: Optional[Dict[str, Any]]) -> str:
    raw: Optional[Any] = None
    if isinstance(config_dict, dict):
        raw = config_dict.get("launch_root") or config_dict.get("project_root")
    if raw:
        return os.path.abspath(os.path.expanduser(os.path.normpath(str(raw))))
    return os.path.abspath(os.getcwd())


def _extra_write_roots(config_dict: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(config_dict, dict):
        return []
    write_guard = config_dict.get("write_guard")
    if not isinstance(write_guard, dict):
        return []
    extra = write_guard.get("extra_paths")
    return [str(x) for x in (extra or []) if x is not None and str(x).strip()]


def os_label() -> str:
    """Человекочитаемое имя ОС из ``sys.platform`` (константа интерпретатора).

    Намеренно НЕ используем ``platform.system()``/``uname()``: на Windows+CPython
    этот путь уходит в WMI-запрос (``platform._wmi_query`` → служба ``winmgmt``),
    который при подвисшей службе блокируется намертво и замораживает старт агента.
    ``sys.platform`` зашит при сборке интерпретатора — без службы и без syscall.
    """
    return host_os_label()


def _coerce_workspace(
    workspace: Optional[Any],
    backend: Optional[Any],
    workspace_config: Optional[Dict[str, Any]],
) -> Any:
    """Build an ExecutionWorkspace; ignore non-sandbox backends (host I/O wins)."""
    if workspace is not None:
        return workspace
    if backend is not None and getattr(backend, "is_sandbox", False):
        return build_execution_workspace(backend, workspace_config)
    # Lazy: avoids tools → security → project_data import cycle.
    from tools.backends.local import LocalFsBackend

    return build_execution_workspace(LocalFsBackend(), workspace_config)


def get_project_data(
    workspace_config: Optional[Dict[str, Any]] = None,
    workspace: Optional[Any] = None,
    backend: Optional[Any] = None,
) -> Dict[str, str]:
    """
    return project data like
    {
      "OS": "Linux",
      "shell": "bash",
      "projectDir": "/home/user/project",
      "projectDirWritable": "true",
      "allowed_write_paths": "/tmp/extra_allowed",
      "currentDate": "2023-10-05",
      "isGitProject": "true",
      "projectDirListing": "folder/\nfolder/file1\nfile2..."
    }

    workspace_config: optional CLI/server config snapshot (launch_root, project_root,
    write_guard.extra_paths) used for projectDir / allowed writes.

    workspace: optional ExecutionWorkspace assembled by RunContext.
    backend: deprecated compat — sandbox backends are coerced into a workspace.

    ``allowed_write_paths`` lists only directories where a probe file can be created and removed
    (same meaning on Linux, macOS, and Windows); non-writable roots are omitted.
    """
    ws = _coerce_workspace(workspace, backend, workspace_config)
    project_dir = ws.root
    be = ws.backend
    current_date = datetime.now().strftime("%Y-%m-%d")
    is_git_project = (
        "true" if be.exists(be.join(project_dir, ".git")) else "false"
    )
    try:
        project_dir_listing = get_listing_from_backend(be, project_dir)
    except OSError:
        project_dir_listing = ""
    root_writable = "true" if ws.root_writable else "false"

    if getattr(be, "is_sandbox", False):
        return {
            "OS": ws.os_name,
            "shell": ws.shell,
            "projectDir": project_dir,
            "projectDirWritable": root_writable,
            "allowed_write_paths": project_dir,
            "currentDate": current_date,
            "isGitProject": is_git_project,
            "projectDirListing": project_dir_listing,
        }

    # Host CLI: extra write roots still probed here (write_guard policy).
    if not os.path.isdir(project_dir):
        project_dir = os.path.abspath(os.getcwd())
        root_writable = "true" if _dir_writable_probe(project_dir) else "false"
        is_git_project = (
            "true" if os.path.exists(os.path.join(project_dir, ".git")) else "false"
        )
        try:
            project_dir_listing = get_listing(project_dir)
        except OSError:
            project_dir_listing = ""

    allowed_roots = _dedupe_ordered_abs_paths(
        [project_dir, *_extra_write_roots(workspace_config)]
    )
    writable_roots = [p for p in allowed_roots if _dir_writable_probe(p)]
    return {
        "OS": ws.os_name,
        "shell": ws.shell,
        "projectDir": project_dir,
        "projectDirWritable": root_writable,
        "allowed_write_paths": "\n".join(writable_roots),
        "currentDate": current_date,
        "isGitProject": is_git_project,
        "projectDirListing": project_dir_listing,
    }
