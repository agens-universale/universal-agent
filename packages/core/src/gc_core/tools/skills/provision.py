"""Скиллы в среде исполнения: копия хостовых слоёв через бэкенд.

Реестр скиллов читает файлы через ``FsBackend``, а слои (system / user /
project) лежат на хосте. У remote-бэкенда (docker / ssh / exec) их «там» нет —
скиллы молча пропадали. Здесь, как в Harbor-адаптере, на старте рана набор
копируется в ``<launch_root>/.gc/skills_runtime/<слой>/`` и реестр собирается
по этим корням. Повторный ран с тем же набором ничего не копирует: манифест
``.manifest.sha256`` сравнивается с хэшем хостовых файлов.

Ограничения (сознательно): без tar — файлы уходят по одному через
``write_bytes`` (десятки файлов, секунды, один раз на набор); symlink'и внутри
скиллов пропускаются (не разыменовываются — проектный скилл из чужого репо не
должен утащить ``~/.aws/credentials`` в контейнер). Копия заливается во
временный каталог и подменяется одним ``rm -rf && mv``: параллельный ран с тем
же корнем не читает полузаписанное дерево.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import posixpath
import shlex
import uuid
from typing import Any, Dict, List, Optional, Tuple

from gc_core.backends.async_protocol import arun_backend_exec_raw
from gc_core.backends.factory import parse_os_backend_kind
from gc_core.tools.skills import registry as skills_registry_module
from gc_core.tools.skills.registry import SkillsRegistry

logger = logging.getLogger(__name__)

RUNTIME_SUBDIR = ".gc/skills_runtime"
MANIFEST_NAME = ".manifest.sha256"
LAYERS = ("system", "user", "project")
#: Только remote-kind: Harbor грузит скиллы адаптером, local/sandbox читают хост.
PROVISIONED_KINDS = frozenset({"docker", "ssh", "exec"})
#: Потолок на весь провижининг: медленный ssh не должен держать старт рана.
PROVISION_TIMEOUT_SEC = 120
#: Не скиллы, а мусор рядом с ними: в копию не идут и в хэш не входят.
SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        "node_modules",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
    }
)


def _host_layer_roots() -> Dict[str, str]:
    # Через модуль, а не прямым импортом: тесты подменяют корни там.
    return {
        "system": skills_registry_module._system_skills_root(),
        "user": skills_registry_module._user_skills_root(),
        "project": skills_registry_module._project_skills_root(),
    }


def _layer_files(layer: str, root: str) -> List[Tuple[str, bytes]]:
    """[(``<слой>/<rel>``, bytes)] одного слоя; ``__pycache__`` пропускается."""
    files: List[Tuple[str, bytes]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            with open(full, "rb") as fh:
                files.append((f"{layer}/{rel}", fh.read()))
    return files


def _collect(roots: Dict[str, str]) -> List[Tuple[str, bytes]]:
    """[(``<слой>/<rel>``, bytes)] по всем существующим слоям, отсортировано."""
    files: List[Tuple[str, bytes]] = []
    for layer in LAYERS:
        root = roots.get(layer)
        if root and os.path.isdir(root):
            files.extend(_layer_files(layer, root))
    files.sort(key=lambda item: item[0])
    return files


def _digest(files: List[Tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for rel, data in files:
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()


def should_provision(config_dict: Dict[str, Any], backend: Any) -> bool:
    """docker/ssh/exec, провижининг не выключен, single-source не задан."""
    if not getattr(backend, "is_sandbox", False):
        return False
    try:
        if parse_os_backend_kind(config_dict) not in PROVISIONED_KINDS:
            return False
    except ValueError:
        return False
    if str(config_dict.get("skills_data_path") or "").strip():
        return False  # Harbor-адаптер и CI: набор уже «там»
    section = config_dict.get("os_backend")
    return not (
        isinstance(section, dict) and section.get("provision_skills") is False
    )


async def _remote_manifest(backend: Any, path: str) -> Optional[str]:
    try:
        return (await backend.read(path)).strip()
    except Exception:
        return None


async def _swap_in(backend: Any, staged: str, root: str) -> None:
    """Подменить живую копию заготовленной одним шагом (rm -rf && mv)."""
    res = await arun_backend_exec_raw(
        backend, f"rm -rf {_q(root)} && mv {_q(staged)} {_q(root)}"
    )
    if res.return_code != 0:
        raise OSError(res.stderr.strip() or f"swap failed: {staged} -> {root}")


def _q(path: str) -> str:
    return shlex.quote(path)


def _remote_root(config_dict: Dict[str, Any], backend: Any) -> Optional[str]:
    """Тот же корень, от которого guards считают ``.gc``: launch_root → project_root → cwd бэкенда."""
    root = config_dict.get("launch_root") or config_dict.get("project_root")
    if not root and callable(getattr(backend, "default_cwd", None)):
        root = backend.default_cwd()
    return str(root) if root else None


async def _upload_set(
    backend: Any, files: List[Tuple[str, bytes]], remote_root: str, digest: str
) -> None:
    staged = f"{remote_root}.staging-{uuid.uuid4().hex[:8]}"
    dirs_done: set[str] = set()
    for rel, data in files:
        target = posixpath.join(staged, rel)
        parent = posixpath.dirname(target)
        if parent not in dirs_done:
            await backend.makedirs(parent)
            dirs_done.add(parent)
        await backend.write_bytes(target, data)
    if not files:
        await backend.makedirs(staged)
    await backend.write(posixpath.join(staged, MANIFEST_NAME), digest + "\n")
    await _swap_in(backend, staged, remote_root)


async def aprovision_remote_skills(
    backend: Any,
    config_dict: Dict[str, Any],
    *,
    run_logger: Optional[logging.Logger] = None,
) -> Optional[SkillsRegistry]:
    """Скопировать слои скиллов в среду исполнения и вернуть реестр по ним.

    ``None`` — провижининг не нужен (хост, Harbor, выключен) или не удался.
    При отказе остаётся реестр по умолчанию: он читает хостовые корни через
    remote-бэкенд и скиллов не найдёт — ран идёт без них, с предупреждением.
    """
    log = run_logger or logger
    if not should_provision(config_dict, backend):
        return None
    launch_root = _remote_root(config_dict, backend)
    if not launch_root:
        return None
    remote_root = posixpath.join(launch_root, RUNTIME_SUBDIR)
    manifest_path = posixpath.join(remote_root, MANIFEST_NAME)

    files = _collect(_host_layer_roots())
    digest = _digest(files)
    try:
        async with asyncio.timeout(PROVISION_TIMEOUT_SEC):
            if await _remote_manifest(backend, manifest_path) == digest:
                log.debug("skills_runtime: up to date at %s", remote_root)
            else:
                await _upload_set(backend, files, remote_root, digest)
                log.info(
                    "skills_runtime: %d files provisioned to %s",
                    len(files),
                    remote_root,
                )
    except Exception as exc:  # провижининг не должен ронять старт рана
        log.warning(
            "skills_runtime: provisioning failed (%s) — skills are unavailable in this run",
            exc,
        )
        return None

    return SkillsRegistry(
        backend=backend,
        system_root=posixpath.join(remote_root, "system"),
        user_root=posixpath.join(remote_root, "user"),
        project_root=posixpath.join(remote_root, "project"),
        disable_skills=skills_registry_module.disabled_skills_from_config(
            config_dict
        ),
    )


__all__ = [
    "LAYERS",
    "MANIFEST_NAME",
    "PROVISIONED_KINDS",
    "RUNTIME_SUBDIR",
    "aprovision_remote_skills",
    "should_provision",
]
