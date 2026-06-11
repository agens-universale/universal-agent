"""Индекс файлов проекта для @-аттача (технический слой, без Textual/бизнес-логики).

Чистые функции: парс активного @-сегмента в вводе, листинг папки, рекурсивный
fuzzy-поиск (git ls-files + fallback walk), резолв @-упоминаний в абсолютные пути,
сборка технического футера. Корень всегда передаётся явно (cwd проекта)."""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional


def parse_at_segment(text: str, cursor: int) -> Optional[tuple[int, str]]:
    """Активный @-сегмент под курсором → (индекс_@, query) или None.

    @ должен стоять в начале строки или после пробела; от @ до cursor — без пробелов.
    query может содержать '/' (навигация по путям)."""
    if cursor < 0 or cursor > len(text):
        return None
    i = cursor - 1
    while i >= 0:
        ch = text[i]
        if ch == "@":
            if i == 0 or text[i - 1].isspace():
                query = text[i + 1:cursor]
                if any(c.isspace() for c in query):
                    return None
                return (i, query)
            return None
        if ch.isspace():
            return None
        i -= 1
    return None


def list_dir(root: str, rel: str) -> list[str]:
    """Содержимое одной папки: папки выше файлов, папки с хвостовым '/'.
    Пути — относительные к root, через '/'. Скрываем только '.git'."""
    base = os.path.join(root, rel) if rel else root
    try:
        names = os.listdir(base)
    except OSError:
        return []
    dirs: list[str] = []
    files: list[str] = []
    rel_prefix = (rel.rstrip("/\\") + "/") if rel else ""
    for name in names:
        if name == ".git":
            continue
        rel_path = (rel_prefix + name).replace("\\", "/")
        if os.path.isdir(os.path.join(base, name)):
            dirs.append(rel_path + "/")
        else:
            files.append(rel_path)
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    return dirs + files


_IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".idea",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
}


def _subseq_match(query: str, path: str) -> bool:
    """query — подпоследовательность path (оба lower)."""
    it = iter(path)
    return all(c in it for c in query)


def rank(query: str, paths: list[str]) -> list[str]:
    """prefix-of-basename выше; затем по глубине; затем по алфавиту.
    Папки (с хвостовым '/') ранжируются по имени папки."""
    q = query.lower().rsplit("/", 1)[-1]

    def key(p: str):
        # Для папок с хвостовым '/' берём последний непустой сегмент.
        base = p.rstrip("/").rsplit("/", 1)[-1].lower()
        return (not base.startswith(q), p.count("/"), p.lower())

    return sorted(paths, key=key)


def _walk_paths(root: str) -> list[str]:
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS]
        for dname in dirnames:
            full = os.path.join(dirpath, dname)
            rel = os.path.relpath(full, root).replace("\\", "/") + "/"
            out.append(rel)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            out.append(rel)
    return out


def _all_paths(root: str) -> list[str]:
    """tracked-файлы git + их папки-префиксы (учтён .gitignore);
    не git-репо → walk + хардкод-игнор."""
    try:
        res = subprocess.run(
            ["git", "ls-files"], cwd=root, capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            files = [ln.strip().replace("\\", "/") for ln in res.stdout.splitlines() if ln.strip()]
            if files:
                dirs: set[str] = set()
                for f in files:
                    parts = f.split("/")
                    for i in range(1, len(parts)):
                        dirs.add("/".join(parts[:i]) + "/")
                return sorted(dirs) + files
    except (OSError, subprocess.SubprocessError):
        pass
    return _walk_paths(root)


def fuzzy_search(root: str, query: str, *, limit: int = 50) -> list[str]:
    """Пустой query → верхний уровень (list_dir). Иначе — рекурсивный fuzzy по файлам."""
    if not query:
        return list_dir(root, "")
    q = query.lower().replace("\\", "/")
    matched = [p for p in _all_paths(root) if _subseq_match(q, p.lower())]
    return rank(query, matched)[:limit]


_AT_MENTION_RE = re.compile(r"(?:^|(?<=\s))@([\w./\\-][\w./\\-]*)")


def extract_at_mentions(text: str) -> list[str]:
    """Все @-упоминания (часть после @, до пробела), триггер — старт строки/после пробела."""
    return [m.group(1) for m in _AT_MENTION_RE.finditer(text)]


def resolve_attachment(root: str, rel: str) -> Optional[tuple[str, str, str]]:
    """rel → (rel_norm, abs_path, kind) если путь существует под root; иначе None.
    kind ∈ {'файл','папка'}. Для папки rel_norm с хвостовым '/'."""
    rel_clean = rel.rstrip("/\\")
    if not rel_clean:
        return None
    full = os.path.normpath(os.path.join(root, rel_clean))
    if os.path.isdir(full):
        return (rel_clean.replace("\\", "/") + "/", full, "папка")
    if os.path.isfile(full):
        return (rel_clean.replace("\\", "/"), full, "файл")
    return None


def build_attachment_footer(resolved: list[tuple[str, str, str]]) -> str:
    """Тело технического футера (без обёртки <technical>). Пусто → ''."""
    if not resolved:
        return ""
    head = ("Пользователь прикрепил через «@» (упоминания @<путь> в предыдущем сообщении "
            "ссылаются на эти файлы/папки относительно корня проекта):")
    lines = [head]
    for rel, abs_path, kind in resolved:
        lines.append(f"- @{rel} → {abs_path} ({kind})")
    return "\n".join(lines)
