"""Плагины из git: клонирование в кэш агента по ``plugins.sources``.

Каждый источник (url + ref) живёт в своём каталоге
``<gc_cache>/plugins/<имя>-<хеш>``. Клон делается один раз, при загрузке
настроек или командой ``gc-plugin``; в цикле агента сеть не используется,
берётся только готовый кэш. Ref закрепляет версию: тег или хеш коммита не меняются сами.
Для ветки (или без ref) обновление явное: ``python -m gc_core.plugins.sources
update`` или удаление каталога кэша. Сбой клона не останавливает агента
(в лог идёт предупреждение, плагин пропускается).
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SHORT = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA = re.compile(r"^[0-9a-f]{7,40}$")
_GIT_TIMEOUT_SEC = 120

_resolved: dict[tuple[str, str, str], Path | None] = {}


def normalize_url(git: str) -> str:
    text = git.strip()
    if _SHORT.match(text) and not Path(text).exists():
        return f"https://github.com/{text}.git"
    return text


def cache_root() -> Path:
    from gc_core.config_modules import env_vars

    return Path(env_vars.gc_paths()["cache"]) / "plugins"


def cache_dir(url: str, ref: str | None) -> Path:
    digest = hashlib.sha256(f"{url}\0{ref or ''}".encode()).hexdigest()[:10]
    stem = re.sub(r"[^0-9A-Za-z_-]+", "_", Path(url.rstrip("/")).stem) or "x"
    return cache_root() / f"{stem}-{digest}"


def _git(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SEC,
    )


def _clone(url: str, ref: str | None, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        if ref and _SHA.match(ref):
            tmp.mkdir()
            _git(["init", "-q"], tmp)
            _git(["remote", "add", "origin", url], tmp)
            _git(["fetch", "-q", "--depth", "1", "origin", ref], tmp)
            _git(["checkout", "-q", "FETCH_HEAD"], tmp)
        else:
            args = ["clone", "-q", "--depth", "1"]
            if ref:
                args += ["--branch", ref]
            _git([*args, url, str(tmp)])
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def resolve_source(
    source: Any, *, update: bool = False, fetch: bool = True
) -> Path | None:
    """Каталог плагина для одного источника; ``None``, если взять не удалось.

    ``fetch=False``: без клона на диске вернуть ``None`` (промах не кэшируется,
    чтобы последующий ``fetch=True`` мог клонировать)."""
    url = normalize_url(source.git)
    key = (url, source.ref or "", source.subdir or "")
    if not update and key in _resolved:
        return _resolved[key]
    target = cache_dir(url, source.ref)
    if not fetch and not update and not (target / ".git").is_dir():
        return None
    try:
        if update or not (target / ".git").is_dir():
            _clone(url, source.ref, target)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        logger.warning("plugin source %s unavailable: %s", url, detail)
        if not (target / ".git").is_dir():
            _resolved[key] = None
            return None
    root = target / source.subdir if source.subdir else target
    result = root if root.is_dir() else None
    if result is None:
        logger.warning("plugin source %s: no directory %s", url, source.subdir)
    _resolved[key] = result
    return result


def resolve_sources(sources: list[Any], *, fetch: bool = True) -> list[str]:
    out: list[str] = []
    for source in sources:
        path = resolve_source(source, fetch=fetch)
        if path is not None:
            out.append(str(path))
    return out


def update_cached() -> list[tuple[str, bool]]:
    """Подтянуть свежий коммит во все закэшированные клоны (для веток)."""
    done: list[tuple[str, bool]] = []
    root = cache_root()
    for target in sorted(root.glob("*")) if root.is_dir() else []:
        if not (target / ".git").is_dir():
            continue
        try:
            _git(["fetch", "-q", "--depth", "1", "origin"], target)
            _git(["reset", "-q", "--hard", "FETCH_HEAD"], target)
            done.append((target.name, True))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("plugin cache %s not updated: %s", target, exc)
            done.append((target.name, False))
    return done


def main(argv: list[str] | None = None) -> int:
    """``update``: обновить все закэшированные плагины из git."""
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["update"]:
        print("usage: python -m gc_core.plugins.sources update")
        return 2
    for name, ok in update_cached():
        print(f"{name}: {'ok' if ok else 'FAILED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
