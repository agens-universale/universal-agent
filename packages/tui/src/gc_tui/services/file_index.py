"""Индекс файлов проекта для @-аттача (технический слой, без Textual/бизнес-логики).

Чистые функции: парс активного @-сегмента в вводе, листинг папки, рекурсивный
поиск по префиксу/подстроке сегмента (PathIndex: git ls-files + fallback-обход
в ширину, собирается один раз, с предохранителем размера), резолв
@-упоминаний в абсолютные пути,
сборка технического футера. Корень всегда передаётся явно (cwd проекта)."""

from __future__ import annotations

import fnmatch
import heapq
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Optional


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
                query = text[i + 1 : cursor]
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
    """Абсолютный, дисковый или уходящий вверх через «..» путь — вне корня
    проекта: пикер такие не подсказывает (внешние пути набираются руками).
    Отдельная Windows-ловушка: join(root, "D:") даёт drive-relative путь —
    «текущую папку диска D», т.е. листался бы корень проекта под видом D:/…
    «..» — то же самое: list_dir(root, "..") листал бы родителя проекта.
    Внутри корня «..» законен («src/../src»), поэтому судим по нормализованному
    пути, а не по наличию «..» в строке. Путь от корня диска («/Users») ловим
    сами: с Python 3.13 isabs на Windows считает его относительным, а join
    по-прежнему даёт D:/Users."""
    if not rel:
        return False
    if rel[0] in "/\\" or os.path.isabs(rel) or _DRIVE_RE.match(rel):
        return True
    norm = os.path.normpath(rel)
    return norm == os.pardir or norm.startswith(os.pardir + os.sep)


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
    папки — её прямые дети в выдаче есть все даже при обрезанном индексе.
    Совпадения по подстроке в других местах (src/ma → packages/core/src/main.py)
    полнотой не обладают: они берутся только из индекса. «gc_tui/scre» при
    отсутствии gc_tui/ в корне — обычный поиск по индексу, хоть в нём и есть '/'."""
    dirpart, sep, _tail = query.replace("\\", "/").rpartition("/")
    if not sep or not dirpart or _is_outside_root(dirpart):
        return False
    return os.path.isdir(os.path.join(root, dirpart))


_IGNORE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
}

_GITIGNORE_MAX_BYTES = 1 << 20
_GLOB_CHARS = frozenset("*?[\\")


def _simple_gitignore_dirs(root: str) -> tuple[frozenset[str], frozenset[str]]:
    """Имена папок, которые проект сам объявил мусором в корневом .gitignore:
    (игнор на любой глубине, игнор только в корне).

    Нужен обходу диска — запасному пути, когда git не уложился в таймаут: иначе
    он, в отличие от git ls-files, тащит в индекс target/, .next/ и т.п. Разбор
    намеренно простой и консервативный: только строки-имена (`out`, `out/`,
    `/out`), без шаблонов, без вложенных .gitignore и глобального ignore. Имя,
    упомянутое в любом отрицании (`!out/`, `!src/out/`, `!OUT`), не игнорируется
    нигде: сверяем последний сегмент отрицания без учёта регистра. При любом
    сомнении (странные пробелы, файл не влез в лимит) папку оставляем: лишний
    мусор в выдаче безобиднее спрятанного кода. Чего разбор не видит: отрицания
    во вложенных .gitignore и файлы, добавленные в git силой внутри игнорируемой
    папки, — они находятся листингом папки, но не поиском."""
    try:
        with open(os.path.join(root, ".gitignore"), "rb") as fh:
            data = fh.read(_GITIGNORE_MAX_BYTES + 1)
    except OSError:
        return frozenset(), frozenset()
    if len(data) > _GITIGNORE_MAX_BYTES:
        return frozenset(), frozenset()  # обрезанная строка стала бы другим именем
    text = data.decode("utf-8", errors="replace")
    anywhere: set[str] = set()
    root_only: set[str] = set()
    negated: list[str] = []
    # split("\n"), не splitlines(): тот режет и по \x0c, \x85, \u2028 внутри имени.
    for raw in text.lstrip("\ufeff").split("\n"):
        if raw.strip().startswith("!"):
            # Отрицание разбираем щедро: лишнее безвредно, пропущенное прячет код.
            # `[^…]` и экранирование fnmatch не понимает — такое отрицание снимает игнор целиком.
            segment = raw.strip()[1:].strip("/").rsplit("/", 1)[-1].lower()
            negated.append("*" if "[" in segment or "\\" in segment else segment)
            continue
        # git снимает один \r и хвостовые пробелы; второй \r — уже часть шаблона
        line = (raw[:-1] if raw.endswith("\r") else raw).rstrip(" ")
        if not line or line.startswith("#"):
            continue
        if line != line.strip() or _GLOB_CHARS & set(line):
            continue
        name = line.rstrip("/")
        if name.startswith("/"):
            name = name[1:]
            target = root_only
        else:
            target = anywhere
        if name and "/" not in name:
            target.add(name)

    def kept(name: str) -> bool:
        return not any(fnmatch.fnmatchcase(name.lower(), pattern) for pattern in negated)

    return (frozenset(n for n in anywhere if kept(n)), frozenset(n for n in root_only if kept(n)))


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


def _entry_is_dir(entry: os.DirEntry) -> bool:
    """Исчезнувшую или недоступную запись показываем как файл, без обхода."""
    try:
        return entry.is_dir()
    except OSError:
        return False


def _walk_paths(
    root: str,
    max_paths: int = MAX_INDEX_PATHS,
    *,
    ignore_anywhere: frozenset[str] = frozenset(),
    ignore_root: frozenset[str] = frozenset(),
) -> tuple[list[str], bool]:
    """Обход В ШИРИНУ с предохранителем → (пути, обрезан ли список).

    В ширину, а не os.walk (он идёт вглубь): верхние уровни ВСЕХ папок попадают
    в индекс раньше глубоких хвостов любой из них — предохранитель срезает
    глубину. Гарантия — порядок по уровням, не полнота уровня, на котором он
    сработал: там записи идут в порядке выдачи диска, и широкая плоская папка
    всё ещё может вытеснить соседей. Симлинк на папку показываем, но внутрь не
    идём (как os.walk без followlinks). `ignore_anywhere`/`ignore_root` —
    имена папок из корневого .gitignore (см. `_simple_gitignore_dirs`),
    пропускаются наравне с `_IGNORE_DIRS`. Начался выход из программы
    (`kill_running_processes`) — обход останавливается на следующей папке и отдаёт
    собранное как обрезанное: дочитывать проект в умирающем процессе некому."""
    out: list[str] = []
    queue: deque[str] = deque([""])
    while queue:
        if _exiting:
            return out, True
        rel_dir = queue.popleft()
        try:
            with os.scandir(os.path.join(root, rel_dir) if rel_dir else root) as it:
                for entry in it:
                    is_dir = _entry_is_dir(entry)
                    if is_dir and (
                        entry.name in _IGNORE_DIRS
                        or entry.name in ignore_anywhere
                        or (not rel_dir and entry.name in ignore_root)
                    ):
                        continue
                    if len(out) >= max_paths:
                        return out, True
                    rel = rel_dir + entry.name
                    if is_dir:
                        out.append(rel + "/")
                        try:
                            is_link = entry.is_symlink()
                        except OSError:
                            is_link = True  # не знаем — внутрь не идём
                        if not is_link:
                            queue.append(rel + "/")
                    else:
                        out.append(rel)
        except OSError:
            continue
    return out, False


class _ShallowestPaths:
    """Отбор самых неглубоких max_paths путей с ограниченной памятью.

    Результат тот же, что у `sorted(paths, key=глубина)[:max_paths]` (сортировка
    устойчива: внутри одной глубины остаются пришедшие раньше), но без хранения
    всего входа: пока путей не больше потолка — обычный список в порядке прихода
    (порядок git, как было раньше); при первом превышении — раскладка по глубине,
    и каждый лишний путь сразу вытесняет самый глубокий из оставленных либо
    отбрасывается сам."""

    def __init__(self, max_paths: int) -> None:
        self._max = max_paths
        self._flat: Optional[list[str]] = []
        self._by_depth: dict[int, list[str]] = {}
        self.truncated = False

    def __len__(self) -> int:
        if self._flat is not None:
            return len(self._flat)
        return sum(len(b) for b in self._by_depth.values())

    def add(self, path: str) -> None:
        if self._max <= 0:
            self.truncated = True
            return
        if self._flat is not None:
            if len(self._flat) < self._max:
                self._flat.append(path)
                return
            for p in self._flat:  # первое превышение: в раскладку
                self._by_depth.setdefault(p.count("/"), []).append(p)
            self._flat = None
        self.truncated = True
        depth = path.count("/")
        deepest = max(self._by_depth)
        if depth >= deepest:
            return  # не мельче самого глубокого — лишний он сам
        bucket = self._by_depth[deepest]
        bucket.pop()  # вытесняем последний из самых глубоких
        if not bucket:
            del self._by_depth[deepest]
        self._by_depth.setdefault(depth, []).append(path)

    def result(self) -> list[str]:
        if self._flat is not None:
            return self._flat
        return [p for depth in sorted(self._by_depth) for p in self._by_depth[depth]]


_GIT_TIMEOUT = 5.0
# GIL освобождается на время самого чтения (сырой read1). Кусок небольшой:
# read1(n) на пустом буфере выделяет байты под весь n при каждом вызове и потом
# обрезает — а пайп всё равно отдаёт только килобайты за раз, крупный n тут
# только лишняя аллокация и копирование.
_GIT_CHUNK = 1 << 16


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Погасить процесс вместе с потомками. Одного proc.kill() мало: git из PATH
    на Windows — обёртка, настоящий git — её дочерний процесс и пережил бы её."""
    try:
        if os.name == "nt":
            # Полный путь: по одному имени Windows ищет exe сначала рядом с
            # приложением и в текущем каталоге — а это может быть открытый проект.
            taskkill = os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe")
            subprocess.run([taskkill, "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=5)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _reap(proc: subprocess.Popen) -> None:
    """Подобрать погашенный процесс, не дольше пары секунд (на POSIX иначе зомби)."""
    try:
        proc.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass


# Процессы, запущенные `_stream_bounded` и ещё не подобранные. При выходе из программы
# их гасит `kill_running_processes`: поток сборки индекса — демон и умрёт вместе с
# процессом, а запущенный им git — нет, он дочитывал бы репозиторий уже без хозяина и
# без таймаута. Снимаются с учёта только после подбора — PID подобранного процесса мог
# бы уже достаться другому.
_running: set[subprocess.Popen] = set()
_running_lock = threading.Lock()
_exiting = False  # выход начался — новых команд не запускаем, обход диска прерываем


def kill_running_processes() -> None:
    """Выход из программы: погасить деревья всех ещё идущих команд индекса, новых не
    запускать и прервать обход диска — и запасной после погашенного git, и уже
    идущий: процессор нужен остальной уборке при выходе. Зовёт `main()` TUI в
    `finally` после `app.run()`."""
    global _exiting
    with _running_lock:
        _exiting = True
        procs = list(_running)
    for proc in procs:
        if proc.poll() is None:
            _kill_process_tree(proc)


def _stream_bounded(cmd: list[str], cwd: str, timeout: float, on_chunk: Callable[[bytes], None]) -> bool:
    """Запустить команду и отдавать её stdout кусками в on_chunk. True — код
    возврата 0; False — ненулевой код, команда не запустилась, или вывод
    кончился, но процесс пришлось гасить принудительно; не уложилась в
    timeout — subprocess.TimeoutExpired. Уже отданные куски при таймауте —
    обрывок: вызывающий обязан их выбросить.

    Пайп читает служебный поток (`_pump`), а не вызывающий: ждать саму
    блокирующую read1 бесполезно ограничивать таймаутом снаружи, а гашение
    дерева (`_kill_process_tree`) — best-effort и может не сработать. Поэтому
    вызывающий ждёт поток с дедлайном (как `communicate` на Windows): не
    дождался — таймаут гарантирован таймингом ожидания, а не тем, получится
    ли убить дерево; `on_chunk` при этом выполняется в служебном потоке.

    Не subprocess.run: на Windows он после kill() ждёт закрытия пайпа без таймаута,
    а пайп держит переживший обёртку дочерний процесс — «таймаут 5 с» превращался
    в ожидание полного прогона git (замер: 19 с на репозитории в 926 тыс. файлов).

    Запущенный процесс до подбора числится в реестре (`_running`) — его погасит
    `kill_running_processes` при выходе из программы; после начала выхода команда не
    запускается вовсе — False."""
    kwargs: dict = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True  # своя группа — чтобы гасить её целиком
    with _running_lock:
        if _exiting:
            return False
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **kwargs)
        except OSError:
            return False
        _running.add(proc)
    try:
        return _pump_bounded(proc, cmd, timeout, on_chunk)
    finally:
        with _running_lock:
            _running.discard(proc)


def _read_process_chunks(proc, stop: threading.Event, on_chunk, failure: list[BaseException]) -> None:
    """Читать stdout в служебном потоке; ошибку передать ожидающему потоку."""
    try:
        while True:
            chunk = proc.stdout.read1(_GIT_CHUNK)
            if not chunk or stop.is_set():
                return
            on_chunk(chunk)
    except BaseException as exc:  # отдадим вызывающему
        failure.append(exc)


def _pump_bounded(proc: subprocess.Popen, cmd: list[str], timeout: float, on_chunk: Callable[[bytes], None]) -> bool:
    """Тело `_stream_bounded` после запуска процесса: чтение служебным потоком с
    дедлайном, гашение и подбор (контракт — там)."""
    failure: list[BaseException] = []
    stop = threading.Event()  # после таймаута вывод никому не нужен

    reader = threading.Thread(
        target=_read_process_chunks, args=(proc, stop, on_chunk, failure), name="file-index-reader", daemon=True
    )
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        stop.set()
        _kill_process_tree(proc)
        reader.join(2)  # пайп закрылся — поток дочитает и выйдет; не вышел — бросаем его (daemon)
        _reap(proc)
        if not reader.is_alive():
            # Закрывать можно только без читателя: он держит лок буфера, close завис бы.
            try:
                proc.stdout.close()
            except OSError:
                pass
        raise subprocess.TimeoutExpired(cmd, timeout)
    try:
        if failure:
            _kill_process_tree(proc)
            _reap(proc)
            raise failure[0]
        try:
            proc.wait(timeout=2)  # вывод кончился — процесс вот-вот выйдет
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            _reap(proc)
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass
    return proc.returncode == 0


def _git_ls_files(root: str, max_paths: int) -> Optional[tuple[list[str], bool]]:
    """Файлы git (tracked + untracked, с учётом .gitignore): (пути, обрезан ли
    список) — не больше max_paths самых неглубоких; None, если git их не дал (не
    репозиторий, git не установлен, ненулевой код); subprocess.TimeoutExpired —
    не уложился.

    -z и байты: с core.quotePath=true (дефолт) git квотит не-ASCII имена
    восьмеричными кодами в текстовом выводе; с -z он их не трогает и разделяет
    NUL. Режем байты по NUL и только потом декодируем как UTF-8: NUL внутри
    многобайтного символа не встречается, поэтому граница куска путь не портит.
    Вывод читается потоком — в памяти не больше max_paths путей, сколько бы
    файлов ни было в репозитории."""
    picked = _ShallowestPaths(max_paths)
    tail = b""

    def on_chunk(chunk: bytes) -> None:
        nonlocal tail
        parts = (tail + chunk).split(b"\0")
        tail = parts.pop()
        for part in parts:
            if part:
                picked.add(part.decode("utf-8", errors="replace"))

    ok = _stream_bounded(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], root, _GIT_TIMEOUT, on_chunk
    )
    if not ok:
        return None
    if tail:
        picked.add(tail.decode("utf-8", errors="replace"))
    return picked.result(), picked.truncated


_SLOW_GIT_RETRY_AFTER = 600.0  # сек: через столько снова пробуем git для «медленного» корня
# Корни, где `git ls-files` не уложился в таймаут → момент таймаута (time.monotonic()).
# Пока срок не вышел, новый индекс того же корня git не зовёт: иначе в огромном
# репозитории каждый новый «@» ждал бы таймаут заново. Не навсегда: разовый
# медленный git не должен оставить репозиторий на обходе диска до конца процесса.
_slow_git_roots: dict[str, float] = {}


def _git_recently_slow(root: str) -> bool:
    at = _slow_git_roots.get(root)
    return at is not None and time.monotonic() - at < _SLOW_GIT_RETRY_AFTER


def _all_paths(root: str, max_paths: int = MAX_INDEX_PATHS, *, use_git: bool = True) -> tuple[list[str], bool]:
    """tracked + untracked файлы git (учтён .gitignore) + их папки-префиксы;
    не git-репо → walk + хардкод-игнор. Возвращает (пути, обрезан ли список).

    --others обязателен: без него untracked-файлы (новые, .claude/ и т.п.)
    видны через drill-in (list_dir читает диск), но не находятся набором —
    «.claude/» листится, «.claude/s» давал «нет совпадений».

    Предохранитель max_paths — по файлам; папки-префиксы идут сверх него
    (обычно их намного меньше). При превышении остаются самые неглубокие файлы — тот
    же принцип, что у _walk_paths: срезаем глубину, а не соседей; для git-ветки
    это делает `_git_ls_files` на лету, пока читает вывод git потоком.

    `git ls-files` вышел по таймауту (огромный репозиторий) → такой корень модуль
    запоминает (`_slow_git_roots`) и `_SLOW_GIT_RETRY_AFTER` секунд git для него не
    зовёт; это единственный след таймаута — отдельного флага у индекса нет, чтобы
    не было второго источника той же правды. `use_git=False` → git не зовётся
    вовсе, сразу walk.

    Обход диска (запасной путь) сверх `_IGNORE_DIRS` уважает простые строки
    корневого .gitignore (см. `_simple_gitignore_dirs`); .gitignore не
    читается, если git ответил успешно."""

    def walk() -> tuple[list[str], bool]:
        anywhere, root_only = _simple_gitignore_dirs(root)
        return _walk_paths(root, max_paths, ignore_anywhere=anywhere, ignore_root=root_only)

    if use_git and not _git_recently_slow(root):
        try:
            listing = _git_ls_files(root, max_paths)
            if listing is not None:
                files, truncated = listing
                if files:
                    dirs: set[str] = set()
                    for f in files:
                        parts = f.split("/")
                        for i in range(1, len(parts)):
                            dirs.add("/".join(parts[:i]) + "/")
                    return sorted(dirs) + files, truncated
        except subprocess.TimeoutExpired:
            _slow_git_roots[root] = time.monotonic()
            return walk()
        except (OSError, subprocess.SubprocessError):
            pass
    return walk()


class PathIndex:
    """Индекс путей одного корня: собирается один раз при первом поиске, дальше
    каждая буква фильтрует готовый список в памяти.

    Потокобезопасен: поиск зовут потоки сеансов пикера (FileSearchSession) — поток
    закрытого сеанса может ещё собирать список, когда поток следующего уже ищет:
    тот ждёт на локе, а не запускает второй обход диска. `truncated` осмыслен после
    сборки — первого `build` или `search`."""

    def __init__(self, root: str, *, max_paths: int = MAX_INDEX_PATHS, use_git: bool = True) -> None:
        self.root = root
        self.truncated = False
        self._max_paths = max_paths
        self._use_git = use_git
        self._paths: Optional[list[str]] = None
        self._lower: list[str] = []
        self._lock = threading.Lock()

    def _ensure_paths(self) -> list[str]:
        with self._lock:
            if self._paths is None:
                paths, truncated = _all_paths(self.root, self._max_paths, use_git=self._use_git)
                lower = [p.lower() for p in paths]
                self._lower = lower
                self.truncated = truncated
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
    head = (
        "Пользователь прикрепил через «@» (упоминания @<путь> в предыдущем сообщении "
        "ссылаются на эти файлы/папки относительно корня проекта):"
    )
    lines = [head]
    for rel, abs_path, kind in resolved:
        lines.append(f"- @{rel} → {abs_path} ({kind})")
    return "\n".join(lines)
