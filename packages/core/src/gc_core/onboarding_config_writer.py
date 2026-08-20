"""Технический хелпер: точечная запись скаляров в config.yml по ключу-якорю
с сохранением комментариев. Бизнес-логики нет."""
import re
from pathlib import Path

from gc_core.utils.atomic_io import atomic_replace


_KEY_LINE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<key>[^\s#][^:\n]*):(?P<rest>.*)$")


def _lines_with_offsets(text: str):
    """Список (start_offset, end_offset, line) для каждой строки с keepends=True."""
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append((pos, pos + len(line), line))
        pos += len(line)
    return out


def _indent_len(line: str) -> int:
    """Длина отступа в символах (пробелы и табы, как есть)."""
    return len(line) - len(line.lstrip(" \t"))


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def set_scalar(text: str, key: str, value: str) -> str:
    """Заменить значение первой незакомментированной строки ``<indent>key:``.

    Закомментированные строки (``# key:``) не матчатся: после отступа идёт
    ``#``, а не ``key``. Бросает KeyError, если строки нет.
    """
    pattern = re.compile(rf"^(?P<indent>[ \t]*){re.escape(key)}:[ \t]*.*$", re.MULTILINE)
    match = pattern.search(text)
    if match is None:
        raise KeyError(key)
    new_line = f'{match.group("indent")}{key}: {_quote(value)}'
    return text[: match.start()] + new_line + text[match.end() :]


def find_anchor(text: str, dotted_key: str):
    """(start, end, indent_str, rest) строки ключа по dotted-пути.

    Каждая часть пути ищется внутри диапазона строк родительской секции
    (до первой не-пустой не-комментарий строки с отступом <= родительского).
    Топ-уровень требует нулевой отступ. KeyError, если части нет.
    """
    parts = dotted_key.split(".")
    lines = _lines_with_offsets(text)
    lo, hi, parent_indent = 0, len(lines), -1
    for depth, part in enumerate(parts):
        found = None
        for i in range(lo, hi):
            _, _, line = lines[i]
            m = _KEY_LINE_RE.match(line)
            if not m or m.group("key").strip() != part:
                continue
            indent = _indent_len(line)
            if depth == 0 and indent != 0:
                continue
            if indent <= parent_indent:
                continue
            found = (i, indent, m)
            break
        if found is None:
            raise KeyError(dotted_key)
        i, indent, m = found
        if depth == len(parts) - 1:
            start, end, _ = lines[i]
            return start, end, m.group("indent"), m.group("rest")
        lo = i + 1
        j = lo
        while j < hi:
            _, _, l2 = lines[j]
            stripped = l2.strip()
            if stripped and not stripped.startswith("#") and _indent_len(l2) <= indent:
                break
            j += 1
        hi = j
        parent_indent = indent
    raise KeyError(dotted_key)  # недостижимо


def section_end(text: str, anchor_end: int, indent_str: str) -> int:
    """Смещение конца блока ключа: до первой не-пустой не-комментарий строки
    с отступом <= отступа ключа (или конец текста)."""
    base = len(indent_str)  # сырые символы — консистентно с _indent_len
    pos = anchor_end
    for start, end, line in _lines_with_offsets(text[anchor_end:]):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and _indent_len(line) <= base:
            return anchor_end + start
        pos = anchor_end + end
    return pos


def set_scalar_at_path(text: str, dotted_key: str, value: str) -> str:
    """Как set_scalar, но адресация dotted-путём (не путает секции)."""
    start, end, indent, _rest = find_anchor(text, dotted_key)
    key = dotted_key.split(".")[-1]
    newline = "\n" if text[start:end].endswith("\n") else ""
    return text[:start] + f"{indent}{key}: {_quote(value)}{newline}" + text[end:]


def _attached_comment_start(text: str, anchor_start: int, indent_str: str) -> int:
    """Начало подряд идущих строк-комментариев с тем же отступом прямо над якорем."""
    lines = _lines_with_offsets(text[:anchor_start])
    cstart = anchor_start
    for start, _end, line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("#") and _indent_len(line) == len(indent_str):
            cstart = start
            continue
        break
    return cstart


def _template_block(template_text: str, dotted_key: str) -> str:
    """Блок ключа в шаблоне: прикреплённые комменты + строка ключа + вложенное."""
    start, end, indent, _rest = find_anchor(template_text, dotted_key)
    block_end = section_end(template_text, end, indent)
    cstart = _attached_comment_start(template_text, start, indent)
    return template_text[cstart:block_end]


def insert_missing(text: str, dotted_key: str, template_text: str) -> str:
    """Вставить отсутствующий ключ блоком из шаблона (с комментами).

    Родительская секция есть → блок в её конец; нет → недостающий уровень
    целиком (из шаблона) в конец файла. Отступы шаблона предполагаются
    едиными с user-файлом (оба — потомки одного шаблона); расхождение
    поймает контрольная валидация записи.
    """
    parts = dotted_key.split(".")
    deepest = 0  # сколько ведущих частей пути уже есть в text
    for k in range(len(parts) - 1, 0, -1):
        try:
            find_anchor(text, ".".join(parts[:k]))
            deepest = k
            break
        except KeyError:
            continue
    block = _template_block(template_text, ".".join(parts[: deepest + 1]))
    if deepest == 0:
        sep = "" if text.endswith("\n") else "\n"
        return text + sep + "\n" + block
    _, p_end, p_indent, _ = find_anchor(text, ".".join(parts[:deepest]))
    insert_at = section_end(text, p_end, p_indent)
    return text[:insert_at] + block + text[insert_at:]


# Живые шаблоны пишут закомментированную секцию как «# key:» — с пробелом после
# решётки; синтетическая форма «#key:» тоже встречается. Понимаем обе.
_COMMENTED_TOP_KEY_RE_TMPL = r"^#[ \t]?(?P<key>{key}):[ \t]*.*$"
_COMMENT_PREFIX_RE = re.compile(r"^#[ \t]?")


def _uncomment(line: str) -> str:
    """Снять префикс комментария: решётку и ОДИН пробел за ней, если он есть.

    Снятие одной решётки оставило бы у «# gc:» ведущий пробел — ключ верхнего
    уровня с отступом ломает YAML, а вложенные строки съезжали бы на столбец
    против канона файла."""
    return _COMMENT_PREFIX_RE.sub("", line, count=1)


def uncomment_template_block(template_text: str, top_key: str) -> str:
    """Блок `#<top_key>:` шаблона: прикреплённое описание (строки `# …` над
    блоком) остаётся комментарием как есть; строки `#<top_key>:` и вложенные
    `#<indent>k: v` — без ведущего '#'.

    Для опциональных секций, которые шаблон несёт ЗАКОММЕНТИРОВАННЫМИ (пример:
    gc — сборка мусора не настраивается «из коробки»). KeyError, если блока нет.
    """
    lines = template_text.splitlines(keepends=True)
    head_re = re.compile(_COMMENTED_TOP_KEY_RE_TMPL.format(key=re.escape(top_key)))
    start = next((i for i, line in enumerate(lines) if head_re.match(line)), None)
    if start is None:
        raise KeyError(top_key)
    # Прикреплённое описание: подряд идущие строки-комментарии прямо над якорем.
    cstart = start
    while cstart > 0 and lines[cstart - 1].startswith("#"):
        cstart -= 1
    # Тело: подряд строки, которые после снятия префикса выглядят вложенными —
    # закомментированные поля (и элементы списка) блока. Отступ и есть признак
    # принадлежности: строка без него — уже следующая секция или разделитель.
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if not line.startswith("#"):
            break
        body = _uncomment(line)
        if not body[:1].isspace() or not _KEY_LINE_RE.match(body):
            break
        end += 1
    out = [lines[i] for i in range(cstart, start)]            # описание — как есть
    out += [_uncomment(lines[i]) for i in range(start, end)]  # шапка + поля — раскомментированы
    return "".join(out)


def write_values(path, values: dict, *, template_path=None, validate=None) -> list[str]:
    """Записать {ключ: значение} в YAML-файл path (атомарно).

    Ключ — dotted-путь (path-aware) или legacy-leaf (первое совпадение).
    Отсутствующий якорь при заданном template_path дописывается блоком из
    шаблона. validate(text) бросает ValueError → файл НЕ записывается.
    Возвращает ключи, которые записать не удалось.
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    template_text = None
    missing: list[str] = []
    for key, value in values.items():
        setter = set_scalar_at_path if "." in key else set_scalar
        try:
            text = setter(text, key, value)
            continue
        except KeyError:
            pass
        if template_path is not None and "." in key:
            if template_text is None:
                template_text = Path(template_path).read_text(encoding="utf-8")
            try:
                text = insert_missing(text, key, template_text)
                text = setter(text, key, value)
                continue
            except KeyError:
                pass
            try:
                find_anchor(text, key.split(".")[0])
            except KeyError:
                # Секции нет в файле вообще (не только у этого ключа) — можно
                # безопасно развернуть блок шаблона. Если секция уже живая
                # (юзер завёл её сам, частично) — трогать текст нельзя: второй
                # top-level блок задублировал бы ключ, а PyYAML на дублях не
                # ругается и молча берёт последний, затирая юзерские значения.
                try:
                    block = uncomment_template_block(template_text, key.split(".")[0])
                    sep = "" if text.endswith("\n") else "\n"
                    text = text + sep + "\n" + block
                    text = setter(text, key, value)
                    continue
                except KeyError:
                    pass
        missing.append(key)
    if validate is not None:
        validate(text)  # ValueError → наружу, файл не тронут
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return missing
