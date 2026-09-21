"""Индекс файлов проекта для @-аттача (технический слой, без Textual/бизнес-логики).

Чистые функции: парс активного @-сегмента в вводе, листинг папки, рекурсивный
поиск по префиксу/подстроке сегмента (PathIndex: git ls-files + fallback-обход
в ширину, собирается один раз, с предохранителем размера), резолв
@-упоминаний в абсолютные пути,
сборка технического футера. Корень всегда передаётся явно (cwd проекта)."""
from __future__ import annotations

import heapq
import os
import re
import signal
import subprocess
import threading
from collections import deque
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


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_outside_root(rel: str) -> bool:
    """Абсолютный или дисковый путь — вне корня проекта: пикер такие не
    подсказывает (внешние пути набираются руками). Отдельная Windows-ловушка:
    join(root, "D:") даёт drive-relative путь — «текущую папку диска D»,
    т.е. листался бы корень проекта под видом D:/…"""
    return bool(rel) and (os.path.isabs(rel) or bool(_DRIVE_RE.match(rel)))


def list_dir(root: str, rel: str) -> list[str]:
    """Содержимое одной папки: папки выше файлов, папки с хвостовым '/'.
    Пути — относительные к root, через '/'. Скрываем только '.git'."""
    if _is_outside_root(rel):
        return []
    base = os.path.join(root, rel) if rel else root
    dirs: list[str] = []
    files: list[str] = []
    rel_prefix = (rel.rstrip("/\\") + "/") if rel else ""
    try:
        with os.scandir(base) as it:
            for entry in it:
                if entry.name == ".git":
                    continue
                rel_path = (rel_prefix + entry.name).replace("\\", "/")
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    dirs.append(rel_path + "/")
                else:
                    files.append(rel_path)
    except OSError:
        return []
    dirs.sort(key=str.lower)
    files.sort(key=str.lower)
    return dirs + files


def names_existing_dir(root: str, query: str) -> bool:
    """query вида «папка/хвост», где «папка» реально существует под root.

    Только такой запрос PathIndex.search дополняет живым листингом названной
    папки — выдача по ней полная даже при обрезанном индексе. «gc_tui/scre» при
    отсутствии gc_tui/ в корне — обычный поиск по индексу, хоть в нём и есть '/'."""
    dirpart, sep, _tail = query.replace("\\", "/").rpartition("/")
    if not sep or not dirpart or _is_outside_root(dirpart):
        return False
    return os.path.isdir(os.path.join(root, dirpart))


_IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".idea",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "dist", "build",
}

# Предохранитель индекса, не размер выдачи (в пикере по-прежнему до 50 строк).
# Без него проект на миллион файлов — минуты сборки и сотни мегабайт даже в фоне,
# плюс секунда фильтра на букву под GIL. 100k — ~2 с обхода и ~0.1 с фильтра.
MAX_INDEX_PATHS = 100_000


def _path_match(query: str, path: str) -> bool:
    """Непрерывное совпадение query с path (оба уже lower).

    Не char-subsequence: иначе «dem» цепляет readme/config через разрозненные
    буквы и короткий ввод почти не фильтрует список.

    - query с '/' → path.startswith(query) или query как подстрока path
      (навигация «src/tu» → src/tui/…);
    - иначе → какой-то сегмент path начинается с query или содержит query
      («sr»→src/, «mai»→main.py, «tui»→…/tui/…).

    Сводится к одному `query in path`: для query с '/' это буквально то же
    самое (startswith — частный случай "in"); для query без '/' вхождение не
    может пересечь границу сегмента (в query нет '/'), поэтому «сегмент
    начинается с query или содержит query» ⇔ «query содержится в path».
    """
    return query in path


def rank(query: str, paths: list[str], *, limit: Optional[int] = None) -> list[str]:
    """prefix-of-basename выше; затем по глубине; затем по алфавиту.
    Папки (с хвостовым '/') ранжируются по имени папки.

    `limit` — эквивалент `sorted(paths, key=key)[:limit]`, но без полной
    сортировки (нужны только первые `limit` элементов)."""
    q = query.lower().rsplit("/", 1)[-1]

    def key(p: str):
        # Для папок с хвостовым '/' берём последний непустой сегмент.
        base = p.rstrip("/").rsplit("/", 1)[-1].lower()
        return (not base.startswith(q), p.count("/"), p.lower())

    if limit is None:
        return sorted(paths, key=key)
    return heapq.nsmallest(limit, paths, key=key)


def _walk_paths(root: str, max_paths: int = MAX_INDEX_PATHS) -> tuple[list[str], bool]:
    """Обход В ШИРИНУ с предохранителем → (пути, обрезан ли список).

    В ширину, а не os.walk (он идёт вглубь): верхние уровни ВСЕХ папок попадают
    в индекс раньше глубоких хвостов любой из них — предохранитель срезает
    глубину. Гарантия — порядок по уровням, не полнота уровня, на котором он
    сработал: там записи идут в порядке выдачи диска, и широкая плоская папка
    всё ещё может вытеснить соседей. Симлинк на папку показываем, но внутрь не
    идём (как os.walk без followlinks)."""
    out: list[str] = []
    queue: deque[str] = deque([""])
    while queue:
        rel_dir = queue.popleft()
        try:
            with os.scandir(os.path.join(root, rel_dir) if rel_dir else root) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir()
                    except OSError:
                        is_dir = False
                    if is_dir and entry.name in _IGNORE_DIRS:
                        continue
                    if len(out) >= max_paths:
                        return out, True
                    rel = rel_dir + entry.name
                    if is_dir:
                        out.append(rel + "/")
                        try:
                            is_link = entry.is_symlink()
                        except OSError:
                            is_link = True      # не знаем — внутрь не идём
                        if not is_link:
                            queue.append(rel + "/")
                    else:
                        out.append(rel)
        except OSError:
            continue
    return out, False


_GIT_TIMEOUT = 5.0


def _kill_process_tree(proc: "subprocess.Popen") -> None:
    """Погасить процесс вместе с потомками. Одного proc.kill() мало: git из PATH
    на Windows — обёртка, настоящий git — её дочерний процесс и пережил бы её."""
    try:
        if os.name == "nt":
            # Полный путь: по одному имени Windows ищет exe сначала рядом с
            # приложением и в текущем каталоге — а это может быть открытый проект.
            taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                    "System32", "taskkill.exe")
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=5)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_bounded(cmd: list[str], cwd: str, timeout: float) -> Optional[bytes]:
    """stdout команды или None при ненулевом коде/ошибке запуска; не уложилась в
    timeout — subprocess.TimeoutExpired: дерево процессов гасится (best-effort),
    а ожидание в любом случае ограничено — таймаут плюс секунды на гашение.

    Не subprocess.run: на Windows он после kill() ждёт закрытия пайпа без таймаута,
    а пайп держит переживший обёртку дочерний процесс — «таймаут 5 с» превращался
    в ожидание полного прогона git (замер: 19 с на репозитории в 926 тыс. файлов)."""
    kwargs: dict = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True      # своя группа — чтобы гасить её целиком
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, **kwargs)
    except OSError:
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=2)         # подобрать процесс; ждать пайп вечно нельзя
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    return out if proc.returncode == 0 else None


def _git_ls_files(root: str) -> Optional[bytes]:
    """Сырой вывод `git ls-files -z` (tracked + untracked, с учётом .gitignore) или
    None, если git его не дал (не репозиторий, git не установлен, ненулевой код);
    subprocess.TimeoutExpired — не уложился.

    -z и байты: с core.quotePath=true (дефолт) git квотит не-ASCII имена
    восьмеричными кодами в текстовом выводе; с -z он их не трогает и разделяет
    NUL. Декодирует вызывающий (git пишет UTF-8, а не локальной кодировкой)."""
    return _run_bounded(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        root, _GIT_TIMEOUT)


def _all_paths(root: str, max_paths: int = MAX_INDEX_PATHS, *,
               use_git: bool = True) -> tuple[list[str], bool, bool]:
    """tracked + untracked файлы git (учтён .gitignore) + их папки-префиксы;
    не git-репо → walk + хардкод-игнор. Возвращает (пути, обрезан ли список,
    не уложился ли git в таймаут).

    --others обязателен: без него untracked-файлы (новые, .claude/ и т.п.)
    видны через drill-in (list_dir читает диск), но не находятся набором —
    «.claude/» листится, «.claude/s» давал «нет совпадений».

    Предохранитель max_paths — по файлам; папки-префиксы идут сверх него
    (обычно их намного меньше). При превышении остаются самые неглубокие файлы — тот
    же принцип, что у _walk_paths: срезаем глубину, а не соседей.

    Третье значение — `git ls-files` вышел по таймауту (огромный репозиторий):
    вызывающий запоминает это и в следующий раз передаёт `use_git=False`,
    чтобы не ждать таймаут на каждый сеанс пикера и не гонять тяжёлый git по
    миллиону файлов впустую. `use_git=False` → git не зовётся вовсе, сразу walk."""
    if use_git:
        try:
            raw = _git_ls_files(root)
            if raw is not None:
                files = [p for p in raw.decode("utf-8", errors="replace").split("\0") if p]
                if files:
                    truncated = len(files) > max_paths
                    if truncated:
                        files = sorted(files, key=lambda f: f.count("/"))[:max_paths]
                    dirs: set[str] = set()
                    for f in files:
                        parts = f.split("/")
                        for i in range(1, len(parts)):
                            dirs.add("/".join(parts[:i]) + "/")
                    return sorted(dirs) + files, truncated, False
        except subprocess.TimeoutExpired:
            paths, truncated = _walk_paths(root, max_paths)
            return paths, truncated, True
        except (OSError, subprocess.SubprocessError):
            pass
    paths, truncated = _walk_paths(root, max_paths)
    return paths, truncated, False


class PathIndex:
    """Индекс путей одного корня: собирается один раз при первом поиске, дальше
    каждая буква фильтрует готовый список в памяти.

    Потокобезопасен: поиск зовут из фоновых потоков (по одному на нажатие) —
    пока первый собирает список, остальные ждут на локе, а не запускают второй
    обход диска. `truncated` и `git_timed_out` осмыслены после сборки — первого
    `build` или `search`."""

    def __init__(self, root: str, *, max_paths: int = MAX_INDEX_PATHS,
                 use_git: bool = True) -> None:
        self.root = root
        self.truncated = False
        self.git_timed_out = False
        self._max_paths = max_paths
        self._use_git = use_git
        self._paths: Optional[list[str]] = None
        self._lower: list[str] = []
        self._lock = threading.Lock()

    def _ensure_paths(self) -> list[str]:
        with self._lock:
            if self._paths is None:
                paths, truncated, git_timed_out = _all_paths(
                    self.root, self._max_paths, use_git=self._use_git)
                lower = [p.lower() for p in paths]
                self._lower = lower
                self.truncated = truncated
                self.git_timed_out = git_timed_out
                self._paths = paths
            return self._paths

    @property
    def built(self) -> bool:
        """Список путей уже собран (сборка идёт один раз, под локом)."""
        return self._paths is not None

    def build(self) -> None:
        """Собрать индекс — или дождаться сборки, начатой другим потоком."""
        self._ensure_paths()

    def search(self, query: str, *, limit: int = 50) -> list[str]:
        """Пустой query → верхний уровень (list_dir). Иначе — рекурсивный поиск по файлам.

        query с '/' дополняется листингом названной папки с диска (фильтр по хвосту):
        инвариант «что показал drill-in, то находит и набор» — иначе gitignored-файлы
        (глобальный ignore, .claude/… и т.п.) листились по «dir/», но пропадали на
        первой же букве фильтра. Глобальный поиск (без '/') ignore уважает — мусор
        вроде .venv в выдачу не лезет."""
        root = self.root
        if not query:
            return list_dir(root, "")
        if _is_outside_root(query):
            return []
        q = query.lower().replace("\\", "/")
        paths = self._ensure_paths()
        matched = [p for p, low in zip(paths, self._lower) if _path_match(q, low)]
        if "/" in q:
            dirpart, _, tail = query.replace("\\", "/").rpartition("/")
            tail_l = tail.lower()
            seen = set(matched)
            for p in list_dir(root, dirpart):
                base = p.rstrip("/").rsplit("/", 1)[-1].lower()
                if (base.startswith(tail_l) or tail_l in base) and p not in seen:
                    matched.append(p)
        return rank(query, matched, limit=limit)


def fuzzy_search(root: str, query: str, *, limit: int = 50) -> list[str]:
    """Разовый поиск без кэша (см. PathIndex.search); пикер держит PathIndex сам."""
    return PathIndex(root).search(query, limit=limit)


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
