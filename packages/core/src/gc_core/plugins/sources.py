"""Плагины из git: клонирование в кэш агента по ``plugins.sources``.

Каждый источник (url + ref) живёт в своём каталоге
``<gc_cache>/plugins/<имя>-<хеш>``. Клон делается один раз, при сборке
снимка набора (``plugins/catalog.py``); в цикле агента сеть не
используется. Ref закрепляет версию: тег или хеш коммита не меняются сами.
Для ветки (или без ref) обновление явное (``update_cached``) или удаление
каталога кэша. Сбой клона не останавливает агента: плагин пропускается,
причина попадает в диагностику снимка.
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
_SHORT_SHA = re.compile(r"^[0-9a-f]{7,39}$")
#: Подсказка к неудачному клону: сервер git отдаёт коммит только по полному
#: хешу, а ``ref`` из 7–39 шестнадцатеричных знаков может быть и тегом.
SHORT_COMMIT_HINT = "если ref — хеш коммита, нужен полный (40 знаков)"
_GIT_TIMEOUT_SEC = 120

_resolved: dict[tuple[str, str, str], Path | None] = {}
_warned: set[tuple[str, str, str]] = set()


def normalize_url(git: str) -> str:
    text = git.strip()
    if _SHORT.match(text) and not Path(text).exists():
        return f"https://github.com/{text}.git"
    return text


def shown_url(text: str) -> str:
    """Адрес источника (или текст с ним) для показа и логов: без учётных
    данных ``user:pass@`` — их хранит только реестр установленных."""
    from gc_core.security.log_redaction import Redactor

    return Redactor().redact(text)


def cache_root() -> Path:
    from gc_core.config_modules import env_vars

    return Path(env_vars.gc_paths()["cache"]) / "plugins"


def cache_dir(url: str, ref: str | None) -> Path:
    digest = hashlib.sha256(f"{url}\0{ref or ''}".encode()).hexdigest()[:10]
    stem = re.sub(r"[^0-9A-Za-z_-]+", "_", Path(url.rstrip("/")).stem) or "x"
    return cache_root() / f"{stem}-{digest}"


def source_dir(source: Any) -> Path:
    """Где лежит плагин источника после клона (без обращения к сети)."""
    target = cache_dir(normalize_url(source.git), source.ref)
    return target / source.subdir if source.subdir else target


def _git(args: list[str], cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SEC,
    )


def is_short_commit(ref: str | None) -> bool:
    """``ref`` похож на короткий хеш коммита (или тег из тех же знаков)."""
    return bool(ref) and _SHORT_SHA.match(ref) is not None


def _clone(url: str, ref: str | None, target: Path) -> None:
    """Клон в ``target``. Не получилось, а ``ref`` похож на короткий хеш —
    ``ValueError`` с подсказкой вместо ошибки git."""
    try:
        _clone_into(url, ref, target)
    except subprocess.SubprocessError as exc:
        if is_short_commit(ref):
            raise ValueError(
                f"коммит {ref} не получен: {SHORT_COMMIT_HINT}"
            ) from exc
        raise


def _clone_into(url: str, ref: str | None, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    # Адрес из конфига — после «--»: значение с «-» не станет опцией git.
    try:
        if ref and _SHA.match(ref):
            tmp.mkdir()
            _git(["init", "-q"], tmp)
            _git(["remote", "add", "origin", "--", url], tmp)
            _git(["fetch", "-q", "--depth", "1", "origin", ref], tmp)
            _git(["checkout", "-q", "FETCH_HEAD"], tmp)
        else:
            args = ["clone", "-q", "--depth", "1"]
            if ref:
                args += ["--branch", ref]
            _git([*args, "--", url, str(tmp)])
        shutil.rmtree(target, ignore_errors=True)
        tmp.rename(target)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _warn_not_cloned(key: tuple[str, str, str], url: str) -> None:
    if key in _warned:
        return
    _warned.add(key)
    logger.warning(
        "plugin source %s is not cloned yet; it is cloned when the "
        "agent starts (plugin snapshot build)",
        shown_url(url),
    )


def resolve_source(
    source: Any, *, update: bool = False, fetch: bool = True
) -> Path | None:
    """Каталог плагина для одного источника; ``None``, если взять не удалось.

    ``fetch=False``: без клона на диске вернуть ``None``. Промах не кэшируется,
    чтобы последующий ``fetch=True`` мог клонировать."""
    url = normalize_url(source.git)
    key = (url, source.ref or "", source.subdir or "")
    if not update and key in _resolved:
        return _resolved[key]
    target = cache_dir(url, source.ref)
    root = source_dir(source)
    if not fetch and not update and not (target / ".git").is_dir():
        _warn_not_cloned(key, url)
        return None
    try:
        if update or not (target / ".git").is_dir():
            _clone(url, source.ref, target)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = shown_url(str(getattr(exc, "stderr", "") or exc))
        # Без клона пользователь получит уведомление набора; здесь — причина.
        if not (target / ".git").is_dir():
            logger.debug(
                "plugin source %s unavailable: %s", shown_url(url), detail
            )
            _resolved[key] = None
            return None
        logger.warning(
            "plugin source %s not updated: %s", shown_url(url), detail
        )
    result = root if root.is_dir() else None
    if result is None:
        logger.debug(
            "plugin source %s: no directory %s", shown_url(url), source.subdir
        )
    _resolved[key] = result
    return result


def is_cloned(source: Any) -> bool:
    """Есть ли на диске клон репозитория источника (без сети)."""
    target = cache_dir(normalize_url(source.git), source.ref)
    return (target / ".git").is_dir()


def is_pinned(clone_dir: Path) -> bool:
    """Клон на detached HEAD (тег или коммит): сам не обновляется."""
    done = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"],
        cwd=clone_dir,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SEC,
    )
    return not done.stdout.strip()


def update_cached() -> list[tuple[str, bool]]:
    """Подтянуть свежий коммит в закэшированные клоны на ветке;
    закреплённые (тег, коммит) не трогаются и в результат не входят."""
    done: list[tuple[str, bool]] = []
    root = cache_root()
    for target in sorted(root.glob("*")) if root.is_dir() else []:
        if not (target / ".git").is_dir():
            continue
        try:
            if is_pinned(target):
                continue
        except (OSError, subprocess.SubprocessError):
            continue
        try:
            _git(["fetch", "-q", "--depth", "1", "origin"], target)
            _git(["reset", "-q", "--hard", "FETCH_HEAD"], target)
            done.append((target.name, True))
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("plugin cache %s not updated: %s", target, exc)
            done.append((target.name, False))
    return done
