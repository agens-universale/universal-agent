"""Тонкие git-обёртки слоя ``agent_edits``: гигиена вызова в одном месте.

Каналы записи журнала и экран обзора правок ходят в git за разным (снимок
``git status``, содержимое индекса, ``git add``), но требования к вызову у них
общие: не писать индекс попусту (``--no-optional-locks``), не искать хуки
репозитория (``core.hooksPath=``) и не звать программу ``core.fsmonitor`` —
конфиг проверяемого репозитория не должен исполнять свой код ни на одном нашем
вызове. Плюс ``preserve_console_mode``: subprocess на Windows гасит ввод мыши
в консоли TUI.

``git status --porcelain`` живёт здесь ЦЕЛИКОМ — и вызов, и разбор строк: его
читают двое (шелл-канал журнала ищет правки команд, экран изменений —
разобранность файлов), и второго разбора porcelain-строк в кодовой базе быть
не должно. C-квотирование путей у git включено по умолчанию, и наивный
``line[3:]`` молча ломает не-ASCII имена.

Слой данных: без UI и без чтения конфига. Никогда не бросает — отказ git
возвращается кодом (``-1`` — процесс не запустился/не ответил), решение
принимает вызывающий.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from gc_core.utils.file_text import (
    BINARY_SAMPLE_BYTES,
    decode_text_best_effort,
)
from gc_core.utils.win_console import preserve_console_mode

_logger = logging.getLogger(__name__)

GIT_TIMEOUT_S = 15

# Потолок длины путей на ОДИН запуск git, когда они идут аргументами (`ls-tree`
# и `diff` читать их из stdin не умеют): предел командной строки Windows —
# около 32 тысяч знаков, с запасом на остальные аргументы. Читается в момент
# вызова (`argv_chunks`), чтобы тесты могли его подменить.
ARGV_CHUNK_CHARS = 16_000

# Кусок пакетных чтений через stdin (`custom_filter_paths`, `blobs_at`): память
# (блобы куска) и время одного запуска под общим `GIT_TIMEOUT_S` — по куску, а
# не по числу путей вызывающего, и отказ одного запуска стоит только куска.
_STDIN_CHUNK_PATHS = 500

# Потолок параллельных запусков git (`run_git_many`): больше ядер процессы
# только толкаются, а десятки одновременных git на Windows забивают диск.
_PARALLEL_GIT = min(8, os.cpu_count() or 1)

_HYGIENE_ARGS = (
    "--no-optional-locks",
    "-c",
    "core.hooksPath=",
    "-c",
    "core.fsmonitor=false",
)

# Без C-квотирования путей: пути мы и передаём, и читаем сами.
_QUOTEPATH_OFF = ("-c", "core.quotepath=false")

# Концы строк формы Windows — для приведения базы к форме файла на диске.
_CRLF = b"\r\n"

# Атрибуты, из-за которых выкладка меняет содержимое сверх концов строк: базу
# из сырого блоба по такому пути собирать нельзя (см. ``has_custom_filter``).
_WORKTREE_ATTRS = ("filter", "working-tree-encoding", "ident")

# Атрибуты, при которых выкладка меняет и концы строк: сырой блоб по такому
# пути не равен форме рабочего дерева (см. ``blobs_filtered_at``).
_EOL_ATTRS = ("text", "eol", "crlf", *_WORKTREE_ATTRS)

# Ложные значения булевой настройки git.
_GIT_FALSE = frozenset({"false", "no", "off", "0"})

# Снимок рабочего дерева. ``-u`` (каждый неотслеживаемый файл, а не каталог
# целиком) нужен шелл-каналу: файл, созданный командой, иначе виден только
# папкой. Аргументы общие обоим читателям — снимок обязан быть одним и тем же.
_STATUS_ARGS = ("status", "--porcelain", "-u")

_RENAME_SEP = " -> "
_OCTAL_DIGITS = "01234567"
_C_ESCAPES = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    '"': 0x22,
    "\\": 0x5C,
}


def run_git(
    launch_root: str,
    args: Sequence[str],
    *,
    timeout: Optional[float] = None,
    stdin: Optional[bytes] = None,
) -> tuple[int, bytes]:
    """``git <гигиена> <args>`` в ``launch_root`` → (код возврата, stdout).

    ``-1`` — git не запустился или не ответил в срок; stdout тогда пуст.

    ``timeout=None`` значит ``GIT_TIMEOUT_S``, и лимит читается в момент
    вызова, а не связывается значением по умолчанию при импорте: иначе подмена
    ``git_ops.GIT_TIMEOUT_S`` (тесты честного отказа git) молча перестала бы
    действовать.
    """
    proc = _run(launch_root, args, timeout, stdin)
    if proc is None:
        return -1, b""
    return proc.returncode, proc.stdout or b""


def status_entries(launch_root: str) -> Optional[List[Tuple[str, str]]]:
    """``git status --porcelain -u`` → ``[(путь, XY)]``; ``None`` — git отказал.

    Отказ обязан отличаться от пустого снимка: для шелл-канала «пусто» значило
    бы, что вся незакоммиченная грязь репозитория — правки текущей команды.
    """
    code, out = run_git(launch_root, list(_STATUS_ARGS))
    if code != 0:
        return None
    rows: List[Tuple[str, str]] = []
    for line in _decode(out).splitlines():
        parsed = parse_status_line(line)
        if parsed is not None:
            rows.append(parsed)
    return rows


def status_entries_for(
    launch_root: str, rels: Sequence[str]
) -> Optional[List[Tuple[str, str]]]:
    """``status_entries``, но только по ``rels``; ``None`` — git отказал.

    Для того, кому нужен статус считанных путей, а не всего проекта: полный
    ``git status -u`` обходит каждый файл, и на проекте в миллион файлов это
    секунды. С путями git смотрит только их. ``:(literal)`` — имя с ``[`` или
    ``*`` не должно разрастаться в шаблон. Пути идут кусками (``argv_chunks``).
    """
    rels = list(rels)
    if not rels:
        return []
    outputs = _chunked_outputs(
        launch_root,
        [*_STATUS_ARGS, "--"],
        [f":(literal){rel}" for rel in rels],
    )
    if outputs is None:
        return None
    rows: List[Tuple[str, str]] = []
    for out in outputs:
        for line in _decode(out).splitlines():
            parsed = parse_status_line(line)
            if parsed is not None:
                rows.append(parsed)
    return rows


def parse_status_line(line: str) -> Optional[Tuple[str, str]]:
    """Строка ``XY <path>`` porcelain v1 → (путь, XY)."""
    if len(line) < 4:
        return None
    xy = line[:2]
    rest = line[3:]
    if "R" in xy or "C" in xy:
        rest = _rename_target(rest)
    path = _unquote_c_path(rest)
    return (path, xy) if path else None


def _rename_target(rest: str) -> str:
    """``old -> new`` → new (rename/copy: интересует новый путь)."""
    if rest.startswith('"'):
        end = _closing_quote(rest)
        tail = rest[end + 1 :] if end > 0 else ""
        if tail.startswith(_RENAME_SEP):
            return tail[len(_RENAME_SEP) :]
        return rest
    head, sep, tail = rest.partition(_RENAME_SEP)
    return tail if sep else head


def _closing_quote(text: str) -> int:
    """Индекс закрывающей кавычки C-quoted токена (экранированные пропускаем)."""
    i = 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i
        i += 1
    return -1


def _unquote_c_path(raw: str) -> str:
    """C-quoted путь git → обычная строка.

    При ``core.quotepath=true`` (дефолт git) не-ASCII имя приходит как
    ``"\\320\\262\\320\\276..."``: октальные эскейпы — это БАЙТЫ utf-8, поэтому
    собираем bytearray и декодируем один раз в конце.
    """
    if not raw.startswith('"'):
        return raw
    end = _closing_quote(raw)
    body = raw[1:end] if end > 0 else raw[1:]

    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        i += 1
        if char != "\\":
            out.extend(char.encode("utf-8"))
            continue
        if i >= len(body):
            break
        escaped = body[i]
        i += 1
        simple = _C_ESCAPES.get(escaped)
        if simple is not None:
            out.append(simple)
            continue
        if escaped in _OCTAL_DIGITS:
            digits = escaped
            while (
                len(digits) < 3 and i < len(body) and body[i] in _OCTAL_DIGITS
            ):
                digits += body[i]
                i += 1
            out.append(int(digits, 8) & 0xFF)
            continue
        out.extend(escaped.encode("utf-8"))
    return out.decode("utf-8", errors="surrogateescape")


# Экранирование git для символов с буквенной формой; прочие управляющие
# (< 0x20 и 0x7f) git пишет трёхзначной восьмеричной (``quote_c_path``).
_C_QUOTED = {
    '"': '\\"',
    "\\": "\\\\",
    "\a": "\\a",
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\v": "\\v",
    "\f": "\\f",
    "\r": "\\r",
}


def _needs_c_quote(char: str) -> bool:
    return char in _C_QUOTED or char < " " or char == "\x7f"


def _c_quote_char(char: str) -> str:
    escaped = _C_QUOTED.get(char)
    if escaped is not None:
        return escaped
    return f"\\{ord(char):03o}" if _needs_c_quote(char) else char


def quote_c_path(path: str) -> str:
    """Имя пути для заголовка патча — C-кавычки, как у git (``quotepath``
    выключен): ``"``, ``\\`` и управляющие символы экранируются, иначе имя
    как есть.

    Обратная ``_unquote_c_path``. Без кавычек ``git apply`` режет имя по
    табуляции. Не-ASCII не экранируется — ``git apply`` принимает его и так.
    """
    if not any(_needs_c_quote(char) for char in path):
        return path
    return '"' + "".join(_c_quote_char(char) for char in path) + '"'


def show_index_bytes(launch_root: str, rel: str) -> Optional[bytes]:
    """``git show :<rel>`` — содержимое пути в ИНДЕКСЕ git.

    ``None`` — пути в индексе нет (неотслеживаемый файл) либо git отказал.
    Путь трактуется git относительно корня рабочего дерева, что совпадает с
    путями из ``git status --porcelain``.
    """
    code, out = run_git(launch_root, [*_QUOTEPATH_OFF, "show", f":{rel}"])
    return out if code == 0 else None


def add_paths(launch_root: str, rels: Sequence[str]) -> tuple[int, str]:
    """``git add -- <rels>`` → (код возврата, текст ошибки git).

    Каждый путь уходит с магией ``:(literal)``: наши ключи — ИМЕНА ФАЙЛОВ, а
    git по умолчанию читает их как pathspec-шаблоны. Живой случай: в
    репозитории лежат ``a1.py`` и ``a[1].py``; ``git add -- "a[1].py"`` без
    литерала трактует скобки как класс символов и стейджит ЧУЖОЙ ``a1.py`` с
    кодом 0 — правку пользователя утащило бы в индекс молча.

    Пустой список путей — no-op: ``git add --`` без путей отказал бы.

    Пути уходят через stdin (``--pathspec-from-file``), а не аргументами:
    предел командной строки Windows — около 32 тысяч знаков, порядка тысячи
    путей, а принятие команды, тронувшей больше, иначе отказало бы целиком.
    Магия ``:(literal)`` из файла читается так же, как из аргументов.
    """
    rels = list(rels)
    if not rels:
        return 0, ""
    specs = _stdin_paths(f":(literal){rel}\x00" for rel in rels)
    proc = _run(
        launch_root,
        [
            *_QUOTEPATH_OFF,
            "add",
            "--pathspec-from-file=-",
            "--pathspec-file-nul",
        ],
        None,
        specs,
    )
    if proc is None:
        return -1, "git не выполнен"
    return proc.returncode, _decode(proc.stderr or b"")


# --- лестница отсчёта: git-факты (§3.2/§5 спеки волны 4) ----------------------


def cat_file_filters(
    launch_root: str, rel: str, *, rev: str = "HEAD"
) -> bytes | None:
    """Содержимое пути в ФОРМЕ РАБОЧЕГО ДЕРЕВА — с eol/ident-фильтрами.

    ``rev="HEAD"`` (по умолчанию) читает объект ``HEAD:<rel>``; ``rev=""`` —
    ``:<rel>`` (индекс, как у ``show_index_bytes``, но пропущенный через
    фильтры). Raw blob тут не годится: у файла с ``eol=crlf`` он вернул бы LF,
    и откат переписал бы файлу переводы строк на Windows. ``None`` — пути на
    этой ступени нет либо git отказал.

    Для ступени коммита и точной базы читайте ``head_bytes_like_disk``: форма
    выкладки и форма файла НА ДИСКЕ совпадают не всегда.
    """
    obj = f"{rev}:{rel}" if rev else f":{rel}"
    code, out = run_git(
        launch_root, [*_QUOTEPATH_OFF, "cat-file", "--filters", obj]
    )
    return out if code == 0 else None


def cat_file_blob(
    launch_root: str, rel: str, *, rev: str = "HEAD"
) -> bytes | None:
    """Сырой блоб ``rev:<rel>`` — БЕЗ фильтров. ``None`` — пути на ступени нет / git отказал."""
    obj = f"{rev}:{rel}" if rev else f":{rel}"
    code, out = run_git(
        launch_root, [*_QUOTEPATH_OFF, "cat-file", "blob", obj]
    )
    return out if code == 0 else None


def disk_eol(data: bytes) -> bytes | None:
    """Преобладающий конец строки: ``b"\\r\\n"``/``b"\\n"``; ``None`` — переводов строк нет."""
    crlf = data.count(_CRLF)
    lf = data.count(b"\n") - crlf
    if crlf == 0 and lf == 0:
        return None
    return _CRLF if crlf >= lf else b"\n"


def with_eol(data: bytes, eol: bytes) -> bytes:
    """Все концы строк → ``eol``; содержимое не трогается."""
    normalized = data.replace(_CRLF, b"\n")
    return normalized if eol == b"\n" else normalized.replace(b"\n", _CRLF)


def head_bytes_like_disk(
    launch_root: str, rel: str, disk: bytes | None, *, rev: str = "HEAD"
) -> bytes | None:
    """Содержимое ``rev:<rel>`` в концах строк файла НА ДИСКЕ.

    ``cat-file --filters`` отдаёт форму, которую создал бы checkout: при
    ``core.autocrlf=true`` это CRLF. Файл, написанный агентом (LF) и
    закоммиченный без выкладки, лежит на диске с LF — сравнение с формой
    фильтра объявляло каждую строку изменённой, а откат переписывал файлу
    концы строк. Поэтому база берётся сырым блобом и приводится к тому, что
    реально на диске: ``disk is None`` (пути на диске нет) — форма фильтра,
    другого свидетельства о «родной» форме нет; бинарь — сырой блоб как есть;
    диск без переводов строк — стиль самого блоба.
    """
    if disk is None:
        return cat_file_filters(launch_root, rel, rev=rev)
    blob = cat_file_blob(launch_root, rel, rev=rev)
    return None if blob is None else blob_like_disk(blob, disk)


def blob_like_disk(blob: bytes, disk: bytes) -> bytes:
    """Сырой блоб в концах строк файла на диске — правило ``head_bytes_like_disk``.

    Отдельно от чтения — для блобов, прочитанных пачкой (``blobs_at``). Бинарь —
    блоб как есть; диск без переводов строк — стиль самого блоба.
    """
    if (
        b"\x00" in blob[:BINARY_SAMPLE_BYTES]
        or b"\x00" in disk[:BINARY_SAMPLE_BYTES]
    ):
        return blob
    eol = disk_eol(disk) or disk_eol(blob) or b"\n"
    return with_eol(blob, eol)


def has_custom_filter(launch_root: str, rel: str) -> bool:
    """Меняет ли выкладка содержимое пути сверх концов строк — по атрибутам.

    Спрашиваются ТРИ атрибута разом: ``filter`` (произвольный smudge/clean),
    ``working-tree-encoding`` (в рабочем дереве файл лежит в другой кодировке)
    и ``ident`` (git разворачивает ``$Id$``). Одного ``filter`` мало:
    ``head_bytes_like_disk`` собирает базу из СЫРОГО блоба и приводит только
    КОНЦЫ СТРОК, а перекодировку и подстановку ``$Id$`` воспроизвести не
    может — путь с ``working-tree-encoding=UTF-16`` получил бы базой
    UTF-8-блоб, и откат записал бы в файл чужую кодировку.

    ``True`` — хоть один атрибут задан (значение не ``unspecified``/``unset``)
    ЛИБО git отказал: конфиг чужого репозитория не должен исполнять свой код
    при показе и учёте диффов, поэтому неопределённость трактуется как
    «фильтр есть» — вызывающий тогда не читает точную базу через
    ``head_bytes_like_disk`` и остаётся на счётной.
    """
    code, out = run_git(
        launch_root, ["check-attr", "-z", *_WORKTREE_ATTRS, "--", rel]
    )
    if code != 0:
        return True
    # ``-z`` печатает тройки «путь, атрибут, значение» — по одной на каждый
    # спрошенный атрибут; значения это каждое третье поле.
    values = _decode(out).split("\x00")[2::3]
    if len(values) < len(_WORKTREE_ATTRS):
        return True
    return any(value not in ("unspecified", "unset") for value in values)


def _stdin_paths(parts: Iterable[str]) -> bytes:
    """Пути для stdin git — байтами имён, как Python кодирует аргументы процессов.

    Имя с байтом не из UTF-8 приходит из ``git status`` суррогатом
    (``_unquote_c_path`` декодирует с ``surrogateescape``) и обязано уйти тем же
    байтом: строгий ``.encode("utf-8")`` кидал на нём исключение — в принятии
    оно роняло TUI.
    """
    return "".join(parts).encode("utf-8", errors="surrogateescape")


def custom_filter_paths(launch_root: str, rels: Sequence[str]) -> set[str]:
    """Пакетный ``has_custom_filter``: какие из ``rels`` меняет выкладка сверх концов строк.

    Один ``git check-attr --stdin`` на всю пачку вместо запуска на путь (замер:
    пофайловые запуски на 2000 путях — 8 с одной этой проверки). Семантика та
    же, вплоть до страховки: git отказал или про путь не ответил — путь считается
    «с фильтром», и точную базу через ``head_bytes_like_disk`` ему не читают.

    Пути уходят через stdin, разделённые NUL (``-z``): ни длина командной
    строки, ни перевод строки в имени пачке не мешают. Кусками по
    ``_STDIN_CHUNK_PATHS``: отказ одного запуска объявляет «с фильтром» только
    свой кусок.
    """
    return set(_set_attrs(launch_root, rels, _WORKTREE_ATTRS))


def worktree_attr_paths(
    launch_root: str, rels: Sequence[str]
) -> tuple[set[str], set[str]]:
    """``(custom_filter_paths, пути, чью выкладку фильтры меняют)`` разом.

    Один ``check-attr`` по атрибутам концов строк (их набор включает
    атрибуты фильтра) и одна проба настройки репозитория вместо двух
    запросов атрибутов — ``custom_filter_paths`` и ``blobs_filtered_at``
    (второе множество передаётся туда в ``affected``). Страховка та же:
    неизвестное — в обоих множествах.
    """
    rels = list(rels)
    if not rels:
        return set(), set()
    attrs = _set_attrs(launch_root, rels, _EOL_ATTRS)
    filtered = {
        rel
        for rel, names in attrs.items()
        if names is None or names & set(_WORKTREE_ATTRS)
    }
    if _config_converts_eol(launch_root):
        return filtered, set(rels)
    return filtered, set(attrs)


def _set_attrs(
    launch_root: str, rels: Sequence[str], attrs: Sequence[str]
) -> dict[str, set[str] | None]:
    """``{путь: заданные атрибуты}`` для путей, несущих хоть один из ``attrs``.

    Задан — значение не ``unspecified`` и не ``unset``. ``None`` — ответ
    неизвестен.

    Один ``git check-attr -z --stdin`` на кусок (``_STDIN_CHUNK_PATHS``). Git
    отказал или про путь ответил не по всем атрибутам — путь в ответе с
    ``None``: вызывающим это безопасная сторона.
    """
    rels = list(rels)
    found: dict[str, set[str] | None] = {}
    for start in range(0, len(rels), _STDIN_CHUNK_PATHS):
        chunk = rels[start : start + _STDIN_CHUNK_PATHS]
        stdin = _stdin_paths(f"{rel}\x00" for rel in chunk)
        code, out = run_git(
            launch_root,
            ["check-attr", "-z", "--stdin", *attrs],
            stdin=stdin,
        )
        if code != 0:
            found.update(dict.fromkeys(chunk))
            continue
        fields = out.decode("utf-8", errors="surrogateescape").split("\x00")
        answers: dict[str, dict[str, str]] = {}
        for i in range(0, len(fields) - 2, 3):
            answers.setdefault(fields[i], {})[fields[i + 1]] = fields[i + 2]
        for rel in chunk:
            names = _names_set(answers.get(rel, {}), attrs)
            if names is None or names:
                found[rel] = names
    return found


def _names_set(
    answer: dict[str, str], attrs: Sequence[str]
) -> set[str] | None:
    """Заданные атрибуты из ответа ``check-attr`` по пути; ``None`` — git
    ответил не по всем спрошенным."""
    if len(answer) < len(attrs):
        return None
    return {
        name
        for name, value in answer.items()
        if value not in ("unspecified", "unset")
    }


def ignored_paths(launch_root: str, rels: Sequence[str]) -> set[str]:
    """Какие из ``rels`` подпадают под правила игнорирования — независимо от индекса.

    ``--no-index``: принудительно проиндексированный путь (``git add -f``) без
    него ответом «не игнорируется» выдавал бы себя за обычный. Один запуск на
    кусок (``_STDIN_CHUNK_PATHS``). Git отказал — весь кусок считается
    игнорируемым: вызывающему это безопасная сторона (путь не выдаётся за
    созданный, и откат его не удаляет).
    """
    rels = list(rels)
    ignored: set[str] = set()
    for start in range(0, len(rels), _STDIN_CHUNK_PATHS):
        chunk = rels[start : start + _STDIN_CHUNK_PATHS]
        stdin = _stdin_paths(f"{rel}\x00" for rel in chunk)
        code, out = run_git(
            launch_root,
            ["check-ignore", "--no-index", "-z", "--stdin"],
            stdin=stdin,
        )
        # Код 1 — «ничего не игнорируется», штатный ответ, а не отказ.
        if code not in (0, 1):
            ignored.update(chunk)
            continue
        ignored.update(
            name
            for name in out.decode("utf-8", errors="surrogateescape").split(
                "\x00"
            )
            if name
        )
    return ignored


def blobs_at(
    launch_root: str, rels: Sequence[str], *, rev: str = "HEAD"
) -> dict[str, bytes | None]:
    """Пакетный ``cat_file_blob``: ``{путь: сырой блоб rev:<путь> или None}``.

    Один ``git cat-file --batch`` на кусок путей (``_STDIN_CHUNK_PATHS``) вместо
    запуска на путь (замер: 2000 путей — 8,6 с пофайловых чтений). ``None`` —
    как у пофайлового чтения: пути на ступени нет, там каталог.

    Путь, чей кусок git не прочитал, в словарь НЕ попадает: ``None`` вызывающий
    запоминает как «пути на ступени нет», и отказ одного запуска молча стирал
    бы ступень у всех путей куска. Пропущенный путь вызывающий спросит по одному.

    Протокол ``--batch`` построчный, поэтому путь с переводом строки в имени
    читается по одному — такие имена редки, а подмешивать их в пачку значило бы
    сдвинуть разбор всех следующих ответов.
    """
    rels = list(dict.fromkeys(rels))
    result: dict[str, bytes | None] = {
        rel: cat_file_blob(launch_root, rel, rev=rev)
        for rel in rels
        if "\n" in rel
    }
    batch = [rel for rel in rels if "\n" not in rel]
    for start in range(0, len(batch), _STDIN_CHUNK_PATHS):
        chunk = batch[start : start + _STDIN_CHUNK_PATHS]
        stdin = "".join(
            f"{rev}:{rel}\n" if rev else f":{rel}\n" for rel in chunk
        )
        code, out = run_git(
            launch_root,
            [*_QUOTEPATH_OFF, "cat-file", "--batch"],
            stdin=_stdin_paths([stdin]),
        )
        blobs = _parse_cat_file_batch(out, len(chunk)) if code == 0 else None
        if blobs is not None:
            result.update(zip(chunk, blobs))
    return result


def _parse_cat_file_batch(out: bytes, count: int) -> list[bytes | None] | None:
    """Ответы ``cat-file --batch`` по порядку запросов; ``None`` — вывод не разобран.

    На объект — строка ``<oid> <тип> <размер>`` и ровно ``размер`` байт плюс
    перевод строки, либо строка ``<имя> missing``. Не блоб (каталог) — ``None``,
    как у ``cat-file blob``.
    """
    items: list[bytes | None] = []
    pos = 0
    for _ in range(count):
        end = out.find(b"\n", pos)
        if end < 0:
            return None
        line, pos = out[pos:end], end + 1
        if line.endswith((b" missing", b" ambiguous")):
            items.append(None)
            continue
        parts = line.split(b" ")
        if len(parts) != 3 or not parts[2].isdigit():
            return None
        size = int(parts[2])
        content = out[pos : pos + size]
        if len(content) != size or out[pos + size : pos + size + 1] != b"\n":
            return None
        pos += size + 1
        items.append(content if parts[1] == b"blob" else None)
    return items


def split_patch_by_path(patch: bytes) -> tuple[dict[str, bytes], bool]:
    """Вывод ``git diff`` по многим путям → ``{путь: его секция, байт в байт}``.

    Секция начинается строкой ``diff --git a/<путь> b/<путь>`` — или, у пути с
    конфликтом слияния, ``diff --cc <путь>`` (``diff --combined``) — и тянется до
    следующего такого заголовка: строка содержимого с ``diff `` начаться не
    может — у неё всегда префикс хэнка (пробел, ``+``, ``-``, ``\\``). Не узнав
    конфликтную секцию, разрезатель терял путь целиком или приклеивал её байты к
    соседней. Секция отдаётся
    БАЙТАМИ: декодировать её надо так же, как ответ пофайлового вызова, иначе
    догадка о кодировке по всей пачке могла бы разойтись с догадкой по файлу.

    Путь достаётся из заголовка при двух условиях вызывающего: префиксы
    ``a/``/``b/`` заданы явно и склейка переименований выключена — тогда обе
    половины заголовка совпадают. Имя в C-кавычках (кавычка, обратный слэш,
    управляющий символ) разбирает ``_unquote_c_path``. Второе значение —
    ``False``, если хоть один заголовок не разобран: вызывающий тогда не вправе
    считать отсутствие секции пустым диффом.
    """
    sections: dict[str, bytes] = {}
    clean = True
    starts = [match.start() for match in _PATCH_HEADER_RE.finditer(patch)]
    if patch and not starts:
        return {}, False
    for i, start in enumerate(starts):
        section = patch[
            start : starts[i + 1] if i + 1 < len(starts) else len(patch)
        ]
        header = section.split(b"\n", 1)[0].decode(
            "utf-8", errors="surrogateescape"
        )
        path = _patch_header_path(header)
        if path is None or path in sections:
            clean = False
            continue
        sections[path] = section
    return sections, clean


_PATCH_HEADER_RE = re.compile(rb"^diff --(?:git|cc|combined) ", re.MULTILINE)


def _patch_header_path(header: str) -> Optional[str]:
    """Заголовок секции диффа → путь; ``None`` — не разобран.

    ``diff --git a/<путь> b/<путь>`` — две половины (голые или в C-кавычках);
    ``diff --cc <путь>`` — одна, без префиксов.
    """
    for combined in ("diff --cc ", "diff --combined "):
        if header.startswith(combined):
            rest = header[len(combined) :]
            return _unquote_c_path(rest) if rest.startswith('"') else rest
    rest = header[len("diff --git ") :]
    if rest.startswith('"'):
        return _quoted_git_header_path(rest)
    if len(rest) < 5 or (len(rest) - 5) % 2:
        return None
    path = rest[2 : 2 + (len(rest) - 5) // 2]
    return path if rest == f"a/{path} b/{path}" else None


def _quoted_git_header_path(rest: str) -> Optional[str]:
    """``"a/<путь>" "b/<путь>"`` в C-кавычках → путь; половины обязаны совпасть."""
    end = _closing_quote(rest)
    if end < 0:
        return None
    src = _unquote_c_path(rest[: end + 1])
    dst = _unquote_c_path(rest[end + 2 :])
    if (
        not (src.startswith("a/") and dst.startswith("b/"))
        or src[2:] != dst[2:]
    ):
        return None
    return src[2:]


def argv_chunks(items: Sequence[str], *, size=len) -> list[list[str]]:
    """``items`` кусками, чтобы аргументы куска укладывались в ``ARGV_CHUNK_CHARS``.

    ``size`` — длина аргумента, которым элемент ляжет в командную строку (путь
    с магией ``:(literal)`` длиннее самого пути). Элемент длиннее куска идёт
    куском в одиночку.
    """
    limit = ARGV_CHUNK_CHARS
    chunks: list[list[str]] = []
    used = 0
    for item in items:
        cost = size(item) + 1
        if not chunks or used + cost > limit:
            chunks.append([])
            used = 0
        chunks[-1].append(item)
        used += cost
    return chunks


def _chunked_outputs(
    launch_root: str, head: Sequence[str], args: Sequence[str]
) -> Optional[list[bytes]]:
    """``git <head> <кусок args>`` по кускам → выводы; ``None`` — хоть один отказал.

    Отказ одного куска — отказ всего вызова: ответ по части путей выдавал бы
    себя за ответ по всем.
    """
    outputs: list[bytes] = []
    for chunk in argv_chunks(args):
        code, out = run_git(launch_root, [*head, *chunk])
        if code != 0:
            return None
        outputs.append(out)
    return outputs


def ls_files_set(launch_root: str, rels: Sequence[str]) -> set[str]:
    """Какие из ``rels`` значатся в индексе (``git ls-files``) — батч-вызовом.

    Без ``:(literal)``: путь с ``[``/``]`` в имени матчится ещё и как glob,
    результат может содержать ЛИШНИЕ чужие пути — безопасно только поэлементным
    ``rel in ls_files_set(..., rels)`` по ТЕМ ЖЕ ``rels``, не итерацией множества.

    Пути идут аргументами кусками (``argv_chunks``): их бывают тысячи.
    """
    rels = list(rels)
    if not rels:
        return set()
    outputs = _chunked_outputs(launch_root, ["ls-files", "-z", "--"], rels)
    if outputs is None:
        return set()
    return {
        path for out in outputs for path in _decode(out).split("\x00") if path
    }


def head_blob_oids(launch_root: str, rels: Sequence[str]) -> dict[str, str]:
    """``{путь: blob-oid в HEAD}`` для ``rels`` — один батч-вызов ``git ls-tree``.

    Путь, отсутствующий в HEAD, в словарь не попадает: по нему лестница
    отсчёта ступени HEAD не строит (§3.2) — отсутствие ключа читается как
    «нет ступени», отдельного кода не заводим. В отличие от ``ls_files_set``,
    у ``ls-tree`` то же glob-расползание pathspec без ``:(literal)`` не
    воспроизводится (путь с ``[``/``]`` лишних ключей не даёт) — ключи словаря
    безопасно использовать напрямую, не только поэлементным lookup'ом.

    Отказ git здесь неотличим от «ни одного пути в HEAD» — кому различие важно,
    зовёт ``head_tree``.
    """
    return head_tree(launch_root, rels) or {}


def head_tree(launch_root: str, rels: Sequence[str]) -> dict[str, str] | None:
    """То же, что ``head_blob_oids``, но отказ git ОТЛИЧИМ: ``None``.

    Нужно тому, кто строит по отсутствию пути ступень «файла нет»: откат к ней
    УДАЛЯЕТ файл, и спутать отказ git с отсутствием значило бы удалить файл
    пользователя.
    """
    rels = list(rels)
    if not rels:
        return {}
    outputs = _chunked_outputs(
        launch_root, ["ls-tree", "-z", "HEAD", "--"], rels
    )
    if outputs is None:
        return None
    oids: dict[str, str] = {}
    for record in (
        rec for out in outputs for rec in _decode(out).split("\x00")
    ):
        if not record:
            continue
        meta, sep, path = record.partition("\t")
        if not sep:
            continue
        oids[path] = meta.rsplit(" ", 1)[-1]
    return oids


def paths_committed_since(
    launch_root: str, rev: str, rels: Sequence[str]
) -> set[str]:
    """Какие из ``rels`` коммит затронул ПОСЛЕ ревизии ``rev`` — один батч-вызов.

    Признак ступени HEAD (§3.2): сравниваются ДЕРЕВЬЯ ``rev`` и ``HEAD``, а не
    даты коммитов — pull и rebase приносят коммиты со старыми датами, и по
    времени признак был бы и ложноположительным, и ложноотрицательным.

    Отказ git (ревизии уже нет после rebase/gc, путь недоступен) — пустое
    множество: ступени HEAD у таких путей не будет. Ложноотрицательный ответ
    стоит лишних строк на экране, ложноположительный — данных пользователя.

    ``:(literal)`` у каждого пути — по той же причине, что в ``add_paths``:
    ключи журнала это ИМЕНА файлов, а не pathspec-шаблоны.
    """
    rels = list(rels)
    if not rels:
        return set()
    specs = [f":(literal){rel}" for rel in rels]
    outputs = _chunked_outputs(
        launch_root,
        [*_QUOTEPATH_OFF, "diff", "--name-only", "-z", rev, "HEAD", "--"],
        specs,
    )
    if outputs is None:
        return set()
    return {
        path for out in outputs for path in _decode(out).split("\x00") if path
    }


@dataclass(frozen=True)
class TreeEntry:
    """Запись дерева ревизии: ``mode`` — строка git (``"100644"``,
    ``"120000"``, ``"160000"``…), ``size`` — байты блоба без фильтров;
    ``None`` у не-блоба (подмодуль, дерево).
    """

    mode: str
    size: int | None


def tree_entries(
    launch_root: str, rels: Sequence[str], *, rev: str
) -> dict[str, TreeEntry] | None:
    """``{путь: TreeEntry}`` для ``rels`` в ревизии ``rev`` — ``ls-tree -l``.

    Пути, которого в ревизии нет, среди ключей нет; отказ git — ``None``
    (отличим от «ни одного пути»).
    """
    rels = list(rels)
    if not rels:
        return {}
    outputs = _chunked_outputs(
        launch_root, ["ls-tree", "-z", "-l", rev, "--"], rels
    )
    if outputs is None:
        return None
    entries: dict[str, TreeEntry] = {}
    for out in outputs:
        for record in _decode(out).split("\x00"):
            meta, sep, path = record.partition("\t")
            fields = meta.split()
            if not sep or len(fields) < 4:
                continue
            size = fields[3]
            entries[path] = TreeEntry(
                fields[0], int(size) if size.isdigit() else None
            )
    return entries


def binary_paths_at(
    launch_root: str, rels: Sequence[str], *, rev: str
) -> set[str] | None:
    """Какие из ``rels`` git считает бинарными в сравнении ``rev`` с диском.

    Один ``diff --numstat`` на кусок путей: бинарная строка — ``-\t-\t<путь>``,
    содержимое в память не читается. Без внешних диффтулов и textconv — как
    у остальных диффов слоя. Отказ git — ``None`` (отличим от «бинарных нет»).
    """
    rels = list(rels)
    if not rels:
        return set()
    outputs = _chunked_outputs(
        launch_root,
        [
            *_QUOTEPATH_OFF,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--numstat",
            "-z",
            rev,
            "--",
        ],
        [f":(literal){rel}" for rel in rels],
    )
    if outputs is None:
        return None
    binary: set[str] = set()
    for out in outputs:
        for row in _decode(out).split("\x00"):
            added, _tab, rest = row.partition("\t")
            deleted, _tab, path = rest.partition("\t")
            if added == "-" and deleted == "-" and path:
                binary.add(path)
    return binary


def blobs_filtered_at(
    launch_root: str,
    rels: Sequence[str],
    *,
    rev: str,
    affected: set[str] | None = None,
) -> dict[str, bytes | None]:
    """``{путь: содержимое в форме рабочего дерева}`` из ревизии ``rev``.

    Путь, чью выкладку фильтры не меняют (``_eol_affected_paths``), — сырой
    блоб, одной пачкой ``blobs_at``. Остальные и те, кого пачка не прочитала,
    — ``cat-file --filters`` ПО ФАЙЛУ, параллельно (``run_git_many``):
    ``--batch --filters`` для них не годится — заголовок пачки несёт размер
    без фильтра, и разбор ломается. Бюджет вызывающего ограничивает байты,
    а не число файлов. ``None`` — git по пути отказал. ``affected`` — уже
    известный ответ ``_eol_affected_paths`` (``worktree_attr_paths``);
    ``None`` — спросить здесь.
    """
    rels = list(rels)
    if affected is None:
        affected = _eol_affected_paths(launch_root, rels)
    result = blobs_at(
        launch_root, [rel for rel in rels if rel not in affected], rev=rev
    )
    per_file = [rel for rel in rels if rel not in result]
    answers = run_git_many(
        launch_root,
        [
            [*_QUOTEPATH_OFF, "cat-file", "--filters", f"{rev}:{rel}"]
            for rel in per_file
        ],
    )
    result.update(
        (rel, out if code == 0 else None)
        for rel, (code, out) in zip(per_file, answers)
    )
    return result


def _eol_affected_paths(launch_root: str, rels: list[str]) -> set[str]:
    """Пути, чью выкладку фильтры меняют: атрибуты концов строк, ``ident``,
    кодировка, фильтр — или настройка репозитория для всех путей сразу
    (``core.autocrlf`` включён, ``core.eol`` задан)."""
    if not rels:
        return set()
    if _config_converts_eol(launch_root):
        return set(rels)
    return set(_set_attrs(launch_root, rels, _EOL_ATTRS))


def _config_converts_eol(launch_root: str) -> bool:
    """``core.autocrlf`` включён или ``core.eol`` задан; отказ git — да."""
    code, out = run_git(
        launch_root,
        ["config", "--get-regexp", r"^core\.(autocrlf|eol)$"],
    )
    if code == 1:
        return False  # ни одного ключа нет
    if code != 0:
        return True
    for line in _decode(out).splitlines():
        key, _sep, value = line.partition(" ")
        if key == "core.eol" or value.lower() not in _GIT_FALSE:
            return True
    return False


def is_ancestor(launch_root: str, older: str, newer: str) -> bool:
    """``older`` — предок ``newer`` (``merge-base --is-ancestor``).

    Отказ git — ``False``.
    """
    code, _out = run_git(
        launch_root, ["merge-base", "--is-ancestor", older, newer]
    )
    return code == 0


def commit_times(launch_root: str, since: str, until: str) -> list[int] | None:
    """Секунды коммиттера коммитов ``since..until``; отказ git — ``None``."""
    code, out = run_git(
        launch_root, ["log", "--format=%ct", f"{since}..{until}"]
    )
    if code != 0:
        return None
    return [int(line) for line in _decode(out).split() if line.isdigit()]


def paths_changed_between(
    launch_root: str, older: str, newer: str
) -> set[str] | None:
    """Пути, различающиеся в деревьях ``older`` и ``newer``; отказ — ``None``.

    В отличие от ``paths_committed_since``, отказ git отличим от пустого
    ответа.
    """
    code, out = run_git(
        launch_root,
        [
            *_QUOTEPATH_OFF,
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            older,
            newer,
        ],
    )
    if code != 0:
        return None
    return {path for path in _decode(out).split("\x00") if path}


def head_rev_from_files(launch_root: str) -> str | None:
    """HEAD-ревизия ЧТЕНИЕМ файлов ``.git`` — БЕЗ запуска git.

    ЕДИНСТВЕННЫЙ источник ревизии для отслеживания правок: её пишет рекордер в
    ``session_head_rev``/отметку создания, её же пишет шелл-канал, и с ней
    сравнивает лестница отсчёта (``_committed_since_snapshot``). Пока источников
    было два, расхождение между ними читалось бы как «коммит тронул путь» там,
    где коммита не было.

    Подпроцесс тут недопустим по двум причинам сразу. Рекордер: признак ступени
    фиксируется в момент съёма ``.orig`` (§3.2), на первом контакте fs-тула с
    файлом, а правок за ход бывают десятки. Экран изменений: ревизия входит в
    ключ кэша вида, который считается ДО проверки кэша, а превью отката зовёт
    вид на каждый путь — ``rev-parse`` стоит десятки миллисекунд, и массовый
    откат замерял 2,2 секунды на шестидесяти файлах.

    Резолвится ровно то, что резолвит git: ``HEAD`` → detached-oid либо
    symref → loose-файл ссылки → ``packed-refs``. Порядок «loose раньше
    packed» — часть контракта: packed-запись может быть протухшей, её oid
    СТАРЕЕ настоящего, а старая ревизия в признаке читается как «коммит был»
    там, где его не было.

    ``None`` — прочитать не удалось (не репозиторий, ветка без коммитов,
    непонятный формат): признак не записывается, ступени HEAD у пути не
    появится вовсе (деградация к поведению без лестницы).
    """
    try:
        git_dir = _git_dir(Path(launch_root))
        if git_dir is None:
            return None
        head = _read_git_line(git_dir / "HEAD")
        if head is None:
            return None
        if not head.startswith("ref:"):
            return head if _is_oid(head) else None
        ref = head[len("ref:") :].strip()
        if not ref:
            return None
        common = _common_dir(git_dir)
        for base in (git_dir, common):
            oid = _read_git_line(base / ref)
            if oid is not None and _is_oid(oid):
                return oid
        return _packed_ref_oid(common, ref)
    except OSError:
        return None


def info_exclude_path(launch_root: str) -> Optional[Path]:
    """Файл правил ``info/exclude`` репозитория — ``None``, если каталога git нет.

    Лежит в ОБЩЕМ каталоге: у связанного worktree правила одни на всех.
    """
    git_dir = _git_dir(Path(launch_root))
    if git_dir is None:
        return None
    return _common_dir(git_dir) / "info" / "exclude"


def _git_dir(root: Path) -> Optional[Path]:
    """``<root>/.git``: каталог, либо файл ``gitdir: <путь>`` (submodule/worktree)."""
    dot = root / ".git"
    if dot.is_dir():
        return dot
    text = _read_git_line(dot)
    if text is None or not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:") :].strip())
    if not target.is_absolute():
        target = root / target
    return target if target.is_dir() else None


def _common_dir(git_dir: Path) -> Path:
    """Общий каталог репозитория: у связанного worktree ссылки лежат ТАМ, а не
    рядом с его собственным ``HEAD``."""
    text = _read_git_line(git_dir / "commondir")
    if not text:
        return git_dir
    common = Path(text)
    return common if common.is_absolute() else git_dir / common


def _packed_ref_oid(common: Path, ref: str) -> Optional[str]:
    """oid ссылки из ``packed-refs``; строки ``#`` (заголовок) и ``^`` (peel) — мимо."""
    text = _read_git_text(common / "packed-refs")
    if text is None:
        return None
    for line in text.splitlines():
        if not line or line[0] in "#^":
            continue
        oid, sep, name = line.partition(" ")
        if sep and name.strip() == ref and _is_oid(oid):
            return oid
    return None


def _read_git_line(path: Path) -> Optional[str]:
    text = _read_git_text(path)
    return None if text is None else text.strip()


def _read_git_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _is_oid(text: str) -> bool:
    """40 hex (sha1) или 64 (репозиторий на sha256) — и ничего кроме."""
    return len(text) in (40, 64) and all(
        ch in "0123456789abcdef" for ch in text
    )


def head_commit_ts(launch_root: str, rel: str) -> float | None:
    """``%ct`` последнего коммита пути — сырое время для сравнений.

    Живёт рядом с ``head_commit_label`` и по той же причине: pathspec нашего
    ключа обязан идти с ``:(literal)``. Без магии путь с ``[``/``]`` матчится
    как glob, и ``git log`` отвечает временем коммита СОСЕДА — а по этому числу
    экран «Хода» решает, показывать ли предупреждение «коммит моложе начала
    хода» (пятый раунд ревью, находка 11).

    Раньше вызов жил в сервисе TUI и магию терял: единственный вызов git по
    нашим путям, который шёл мимо этого модуля. Выбор ступени лестницы (§3.2)
    временем коммита по-прежнему не решается — там blob-oid.
    """
    code, out = run_git(
        launch_root, ["log", "-1", "--format=%ct", "--", f":(literal){rel}"]
    )
    if code != 0:
        return None
    text = _decode(out).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def head_commit_label(launch_root: str, rel: str) -> str | None:
    """Подпись последнего коммита пути: ``"e5f2c1a · 14:20"`` (локальное время).

    ТОЛЬКО для текста превью отката. Выбор ступени лестницы (§3.2) идёт по
    blob-oid (``head_blob_oids``) — время коммита в нём не участвует: pull и
    rebase приносят коммиты со старыми датами. ``git log`` без истории пути
    отвечает кодом 0 и ПУСТЫМ выводом — это тоже ``None``, а не ошибка разбора.
    """
    code, out = run_git(launch_root, _last_commit_args(rel))
    commit = _parse_last_commit(out) if code == 0 else None
    return None if commit is None else commit_label(*commit)


def _last_commit_args(rel: str) -> list[str]:
    """``git log -1`` пути — одна команда для подписи по одному и для пачки на слияниях.

    ``:(literal)`` — по той же причине, что в ``add_paths``: без магии путь с
    ``[``/``]`` матчится как glob, и подписью пути стал бы коммит соседа.
    """
    return ["log", "-1", "--format=%h\t%ct", "--", f":(literal){rel}"]


def _parse_last_commit(out: bytes) -> tuple[str, int] | None:
    """Вывод ``_last_commit_args`` → ``(короткий хэш, %ct)``; ``None`` — истории нет."""
    text = _decode(out).strip()
    short_hash, sep, ts_text = text.partition("\t")
    if not sep or not ts_text.isdigit():
        return None
    return short_hash, int(ts_text)


def commit_label(short_hash: str, ts: int) -> str:
    """Подпись коммита для превью отката: ``"e5f2c1a · 14:20"`` (локальное время)."""
    return f"{short_hash} · {datetime.fromtimestamp(ts):%H:%M}"


def last_commits(
    launch_root: str, since: str, rels: Sequence[str]
) -> dict[str, tuple[str, int]]:
    """``{путь: (короткий хэш, %ct)}`` последнего коммита пути в ``since..HEAD``.

    Пакетный ``head_commit_label``/``head_commit_ts`` для путей, которые коммит
    тронул ПОСЛЕ ``since`` (``paths_committed_since``): их последний коммит лежит
    в этом диапазоне, и один ``git log`` на кусок путей заменяет ``git log -1``
    на каждый (замер: 2000 путей — 26 с открытия экрана изменений). Первое
    появление пути в выводе, от новых коммитов к старым, — его последний коммит.

    Путь без ответа (git отказал, коммита в диапазоне нет) в словарь не попадает
    — вызывающий спросит его по одному. Заголовок коммита помечен байтом
    ``\\x01``: с именем файла его не спутать.

    Один ``git log`` — только для ЛИНЕЙНОГО диапазона. Одиночный
    ``git log -1 -- путь`` упрощает историю по одному пути и на слиянии идёт за
    родителем, от которого путь пришёл; пачка упрощает её по всем путям разом, а
    ``--name-only`` у слияния имён не печатает — подписью разрешённого конфликта
    стал бы коммит-родитель. Есть слияние в диапазоне — каждый путь спрашивается
    той же одиночной командой, что и по одному (``_last_commit_args``), но
    параллельно (``run_git_many``): ответ совпадает, а обычный ``git pull`` со
    слиянием не возвращает экран к последовательным запускам.
    """
    rels = list(rels)
    if not rels:
        return {}
    code, merges = run_git(
        launch_root, ["rev-list", "--merges", "-n", "1", f"{since}..HEAD"]
    )
    if code != 0:
        return {}
    if merges.strip():
        return _last_commits_one_by_one(launch_root, rels)
    wanted = set(rels)
    specs = [f":(literal){rel}" for rel in rels]
    outputs = _chunked_outputs(
        launch_root,
        [
            *_QUOTEPATH_OFF,
            "log",
            "--format=%x01%h%x09%ct",
            "--name-only",
            "-z",
            f"{since}..HEAD",
            "--",
        ],
        specs,
    )
    found: dict[str, tuple[str, int]] = {}
    for out in outputs or ():
        _collect_last_commits(out, wanted, found)
    return found


def _last_commits_one_by_one(
    launch_root: str, rels: list[str]
) -> dict[str, tuple[str, int]]:
    """Диапазон со слиянием: каждый путь — своей одиночной командой, параллельно."""
    answers = run_git_many(
        launch_root, [_last_commit_args(rel) for rel in rels]
    )
    return {
        rel: commit
        for rel, (answer_code, out) in zip(rels, answers)
        if answer_code == 0 and (commit := _parse_last_commit(out)) is not None
    }


def _collect_last_commits(
    out: bytes, wanted: set[str], found: dict[str, tuple[str, int]]
) -> None:
    """Вывод ``git log --name-only -z`` → первый (последний) коммит пути в ``found``."""
    commit: tuple[str, int] | None = None
    first_name = False
    for token in out.decode("utf-8", errors="surrogateescape").split("\x00"):
        if token.startswith("\x01"):
            short_hash, _sep, ts_text = token[1:].partition("\t")
            commit = (short_hash, int(ts_text)) if ts_text.isdigit() else None
            first_name = True
            continue
        # Первое имя после заголовка git отделяет переводом строки.
        name = token[1:] if first_name and token.startswith("\n") else token
        first_name = False
        if commit is not None and name in wanted and name not in found:
            found[name] = commit


def run_git_many(
    launch_root: str, argvs: Sequence[Sequence[str]]
) -> list[tuple[int, bytes]]:
    """Независимые запуски git параллельно → ``[(код, stdout)]`` в порядке ``argvs``.

    Для того, что одним вызовом git не собрать (дифф нового файла: ``--no-index``
    сравнивает ровно два пути), а по одному стоит слишком дорого — запуск на
    путь, тысячи путей.

    Режим консоли сохраняется ОДИН раз на всю пачку, а не вокруг каждого
    запуска: у параллельных охранников второй запомнил бы режим, уже
    испорченный чужим дочерним процессом (на Windows он гасит мышь), и под
    конец вернул бы именно его.
    """
    argvs = [list(args) for args in argvs]
    if not argvs:
        return []

    def one(args: list[str]) -> tuple[int, bytes]:
        proc = _run_unguarded(launch_root, args, None, None)
        return (
            (-1, b"")
            if proc is None
            else (proc.returncode, proc.stdout or b"")
        )

    workers = min(_PARALLEL_GIT, len(argvs))
    with (
        preserve_console_mode(),
        ThreadPoolExecutor(max_workers=workers) as pool,
    ):
        return list(pool.map(one, argvs))


def _run(
    launch_root: str,
    args: Sequence[str],
    timeout: Optional[float],
    stdin: Optional[bytes] = None,
) -> Optional[subprocess.CompletedProcess]:
    with preserve_console_mode():
        return _run_unguarded(launch_root, args, timeout, stdin)


def _run_unguarded(
    launch_root: str,
    args: Sequence[str],
    timeout: Optional[float],
    stdin: Optional[bytes],
) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *_HYGIENE_ARGS, *args],
            cwd=launch_root,
            capture_output=True,
            input=stdin,
            timeout=GIT_TIMEOUT_S if timeout is None else timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _logger.warning("agent_edits: git %s не выполнен: %s", list(args), exc)
        return None


def _decode(data: bytes) -> str:
    """Байты git → текст: явный декод, без опоры на локаль."""
    if not data:
        return ""
    return decode_text_best_effort(data) or data.decode(
        "utf-8", errors="replace"
    )
