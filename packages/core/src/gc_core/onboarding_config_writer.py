"""Технический хелпер: точечная запись скаляров в config.yml по ключу-якорю
с сохранением комментариев. Бизнес-логики нет — кроме `write_profile_pointer`:
склейку примитивов указателя (якорь → значение → валидация стека) держим здесь
одну на всех, потому что её потребители порознь уже разъезжались."""

import re
from pathlib import Path

import yaml

from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.user_text import read_user_text

# Ключ строки — либо квотированный скаляр ЦЕЛИКОМ, либо плоский до первого
# двоеточия. Квотированные формы идут первыми не для красоты: `"llama3:8b":`
# (Ollama-стиль, схема такой алиас пропускает) плоская ветка резала по первому
# двоеточию до `"llama3`, и писатель переставал находить то, что сам записал.
_KEY_LINE_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<key>\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^\s#][^:\n]*)"
    r":(?P<rest>.*)$"
)


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


def _key_scalar(raw: str) -> str:
    """Ключ как YAML-СКАЛЯР, а не как байты строки файла.

    Писатель обязан находить всё, что видит загрузчик (`dict[str, ModelEntry]` —
    любой строковый ключ), а один и тот же ключ живёт в файле в разных начертаниях:
    писатель кладёт `"llama3:8b"`, рука пишет `'123'` или `'*'`. Побайтное
    сравнение разводило эти виды — запись «не находилась», и upsert дописывал
    дубль вместо замены. Разбор не удался (обрывок вроде `*`) — сравниваем
    литералом: хуже прежнего не будет."""
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    return raw if value is None else str(value)


def quote_scalar(value: str) -> str:
    """Двойные кавычки YAML-строки с escape `\\`/`"` — публичный, т.к. нужен и
    вызывающим, собирающим свои block_lines для upsert_mapping_entry (слой записи
    реестра, `config_modules/registry_write.py`), не только сеттерам этого модуля."""
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
    new_line = f"{match.group('indent')}{key}: {quote_scalar(value)}"
    return text[: match.start()] + new_line + text[match.end() :]


def _find_key_line(lines: list, lo: int, hi: int, part: str, depth: int, parent_indent: int):
    """(индекс, отступ, match) первой строки ключа `part` в диапазоне; None, если нет."""
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
        return i, indent, m
    return None


def _section_end_index(lines: list, lo: int, hi: int, indent: int) -> int:
    """Индекс первой значащей строки с отступом <= indent (конец секции), либо hi."""
    j = lo
    while j < hi:
        _, _, l2 = lines[j]
        stripped = l2.strip()
        if stripped and not stripped.startswith("#") and _indent_len(l2) <= indent:
            break
        j += 1
    return j


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
        found = _find_key_line(lines, lo, hi, part, depth, parent_indent)
        if found is None:
            raise KeyError(dotted_key)
        i, indent, m = found
        if depth == len(parts) - 1:
            start, end, _ = lines[i]
            return start, end, m.group("indent"), m.group("rest")
        lo = i + 1
        hi = _section_end_index(lines, lo, hi, indent)
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
    return text[:start] + f"{indent}{key}: {quote_scalar(value)}{newline}" + text[end:]


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


def _exclude_next_attached_comment(text: str, raw_end: int) -> int:
    """`raw_end` (обычно из `section_end`) сдвинутый назад, если он упирается в
    ПРИКРЕПЛЁННЫЙ комментарий следующей записи/секции — тот комментарий
    принадлежит ей, а не текущему диапазону (симметрично `_template_block`,
    который так же защищает прикреплённые комментарии на чтении, только там в
    сторону начала, здесь — в сторону конца)."""
    if raw_end >= len(text):
        return raw_end
    line_end = text.find("\n", raw_end)
    next_line = text[raw_end:] if line_end == -1 else text[raw_end : line_end + 1]
    return _attached_comment_start(text, raw_end, " " * _indent_len(next_line))


def _template_block(template_text: str, dotted_key: str) -> str:
    """Блок ключа в шаблоне: прикреплённые комменты + строка ключа + вложенное."""
    start, end, indent, _rest = find_anchor(template_text, dotted_key)
    block_end = section_end(template_text, end, indent)
    cstart = _attached_comment_start(template_text, start, indent)
    return template_text[cstart:block_end]


# Топ-уровневый ключ шаблона в ЛЮБОЙ из двух форм: живой (`level:`) и
# закомментированный (`# models:` / `#models:` — так шаблон документирует
# опциональные секции). Вложенные строки (`  use_ssl:`, `#   glm-local:`) не
# матчатся: после необязательных '#' и ОДНОГО пробела обязан идти сам ключ.
_TOP_KEY_LINE_RE = re.compile(r"^#? ?(?P<key>[A-Za-z_][A-Za-z0-9_.\-]*):")


def _template_top_keys(template_text: str) -> list[str]:
    """Порядок топ-уровневых ключей шаблона (первое вхождение каждого)."""
    keys, seen = [], set()
    for line in template_text.splitlines():
        match = _TOP_KEY_LINE_RE.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _top_level_insert_at(text: str, key: str, template_text: str):
    """Смещение вставки нового ТОП-уровневого ключа `key`: сразу за блоком
    ближайшего предшествующего (по порядку шаблона) ключа, который в `text` уже
    есть. Соседа нет — None, вызывающий дописывает в конец файла.

    Иначе новые указатели/секции валились в хвост файла — `level:` оказывался за
    сотней строк от `profile:`, с которым читается в паре."""
    keys = _template_top_keys(template_text)
    if key not in keys:
        return None
    for neighbour in reversed(keys[: keys.index(key)]):
        try:
            _n_start, n_end, n_indent, _n_rest = find_anchor(text, neighbour)
        except KeyError:
            continue
        # Комментарий, прикреплённый к СЛЕДУЮЩЕЙ секции, принадлежит ей —
        # вставляемый блок встаёт перед ним, а не между ним и его ключом.
        return _exclude_next_attached_comment(text, section_end(text, n_end, n_indent))
    return None


def _splice(text: str, insert_at: int, block: str) -> str:
    """Вставить блок по смещению, не склеив его строку с соседской.

    Перевод строки обязан быть с обеих сторон шва: у самого блока (хвост шаблона
    мог остаться без финального `\\n`) и у текста перед ним. Второе — не теория:
    `insert_at` равен длине текста, когда сосед оказался ПОСЛЕДНИМ ключом файла,
    а файл сохранён без завершающего перевода строки (обычный итог ручной
    правки). Без этого выходило `profile: "corp"level: default` — сломанный YAML,
    причём `ensure_pointer_anchor` пишет без валидатора, и откатить порчу было бы
    некому."""
    if not block.endswith("\n"):
        block += "\n"
    prefix = text[:insert_at]
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + block + text[insert_at:]


def insert_missing(text: str, dotted_key: str, template_text: str) -> str:
    """Вставить отсутствующий ключ блоком из шаблона (с комментами).

    Родительская секция есть → блок в её конец; нет → недостающий уровень
    целиком (из шаблона) за блоком соседа по порядку шаблона (соседа в файле
    нет — в конец файла). Отступы шаблона предполагаются едиными с user-файлом
    (оба — потомки одного шаблона); расхождение поймает контрольная валидация
    записи.
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
        insert_at = _top_level_insert_at(text, parts[0], template_text)
        if insert_at is None:
            sep = "" if text.endswith("\n") else "\n"
            return text + sep + "\n" + block
    else:
        _p_start, p_end, p_indent, _p_rest = find_anchor(text, ".".join(parts[:deepest]))
        insert_at = section_end(text, p_end, p_indent)
    return _splice(text, insert_at, block)


def _child_indent_len(segment: str, parent_indent: int):
    """Колонка ПРЯМЫХ детей секции: минимальный отступ среди строк-ключей глубже
    родителя (None — детей нет). Один источник и для поиска записи, и для вставки
    новой: колонку задаёт живой сосед, а не хардкод «родитель + 2» — в файле с
    4-пробельным каноном тот выдавал запись на чужой колонке (YAML ломался либо
    запись вкладывалась внутрь соседней, и секция «Модели» не работала вовсе)."""
    child = None
    for _start, _end, line in _lines_with_offsets(segment):
        if _KEY_LINE_RE.match(line) is None:
            continue
        indent = _indent_len(line)
        if indent <= parent_indent:
            continue
        if child is None or indent < child:
            child = indent
    return child


def _find_child_anchor(text: str, parent_dotted: str, key: str):
    """(start, end, indent_str, rest) строки ПРЯМОГО ребёнка `key` секции
    `parent_dotted`. Ключ сравнивается ЦЕЛИКОМ, НЕ через dotted-путь: алиасы
    с точками (`opus-4.8`) в `find_anchor` разбивались на вложенный путь
    opus-4→8, запись «не находилась» — upsert дописывал дубль, remove молчал
    (смок 2026-07-30). Прямой ребёнок = ключ-строка минимального отступа внутри
    диапазона секции (глубже отступа родителя) — вложенное поле-тёзка (`model:`
    внутри чужой записи) не матчится. Сам ключ сверяется КАК СКАЛЯР (`_key_scalar`):
    начертание в файле и начертание, которым его ищут, совпадают не всегда.
    KeyError, если ребёнка нет."""
    _p_start, p_end, p_indent, _p_rest = find_anchor(text, parent_dotted)
    span_end = section_end(text, p_end, p_indent)
    seg = text[p_end:span_end]
    child_indent = _child_indent_len(seg, len(p_indent))
    if child_indent is not None:
        wanted = _key_scalar(key)
        for start, end2, line in _lines_with_offsets(seg):
            m = _KEY_LINE_RE.match(line)
            if m is None or _indent_len(line) != child_indent:
                continue
            if _key_scalar(m.group("key").strip()) == wanted:
                return p_end + start, p_end + end2, m.group("indent"), m.group("rest")
    raise KeyError(f"{parent_dotted}.{key}")


# Хвост строки родителя (`parent:<rest>`) БЕЗ значения: голый ключ либо пустой
# инлайновый мэппинг `{}`, в обоих случаях с необязательным трейлинг-комментом.
# Всё прочее в этой позиции — рукописный НЕПУСТОЙ инлайновый мэппинг: у его
# записей нет своих строк, и построчный писатель их не тронет.
_EMPTY_PARENT_REST_RE = re.compile(r"^(?:[ \t]*\{[ \t]*\})?(?P<comment>[ \t]*#.*)?[ \t]*$")


def _empty_parent_tail(rest: str, parent_dotted: str) -> str:
    """Трейлинг-комментарий строки пустого родителя (или ''), с отказом на
    непустом инлайновом мэппинге.

    Отказ — тот же контракт, что у `_drop_mapping_entry` слоя записи реестра:
    форму, у которой отдельной строки нет вовсе, построчный писатель обязан
    назвать, а не «починить». Разбирать и разворачивать flow-мэппинг он не
    станет — это не его работа; молча переписать секцию блочной формой значило бы
    снести соседние записи под зелёный нотис. Текст отказа называет ФАКТ (всё
    содержимое секции лежит в строке ключа), а инлайновый мэппинг — гипотезой:
    это самая частая, но не единственная форма такой строки."""
    match = _EMPTY_PARENT_REST_RE.match(rest)
    if match is None:
        raise ValueError(
            f"содержимое секции {parent_dotted} лежит в строке самого ключа, а правки "
            "идут построчно; похоже, это инлайновый мэппинг — разверните секцию в "
            "блочную форму (каждая запись со своей строки)"
        )
    return match.group("comment") or ""


def upsert_mapping_entry(text: str, parent_dotted: str, key: str, block_lines: list[str]) -> str:
    """Вставить/заменить запись словаря (алиас реестра, слот, привязку) целым
    блоком под родительским якорем `parent_dotted` (например ``"models"`` или
    ``"assign.subagents"``).

    `block_lines` — строки блока БЕЗ отступа родителя (первая строка — сама
    `key:`, остальные — её вложенное содержимое с СОБСТВЕННЫМ относительным
    отступом); функция доиндентивает каждую строку на колонку УЖЕ ЛЕЖАЩИХ
    соседей (родитель + 2 — только когда соседей нет, `_child_indent_len`).
    Родителя нет в `text` → `KeyError` (вызывающий сначала гарантирует якорь
    через `ensure_section_anchor`). Родитель пуст (`key:` без детей ИЛИ
    `key: {}`) → первая запись, инлайновая `{}` нормализуется в блочную форму, а
    трейлинг-комментарий строки родителя переезжает вместе с ней. Родитель —
    НЕПУСТОЙ инлайновый мэппинг → `ValueError` (`_empty_parent_tail`).
    Запись `key` уже есть → заменяется ЦЕЛИКОМ БЛОКОМ, включая её СОБСТВЕННЫЙ
    прикреплённый комментарий (тот уходит вместе со старым блоком — если нужен
    новый, положите его первой строкой(-ами) `block_lines`); прикреплённый
    комментарий СЛЕДУЮЩЕЙ записи не трогается — он ей и принадлежит (в т.ч. на
    ВСТАВКЕ новой записи в непустого родителя — `_exclude_next_attached_comment`,
    иначе комментарий соседней топ-уровневой секции переехал бы ПЕРЕД новую
    запись при добавлении второй+ записи подряд, напр. models.light вслед за
    models.default). Хвостовой «шум» за последней содержательной строкой записи
    (пустые строки, висячий комментарий, отделённый пустой строкой) записи НЕ
    принадлежит — граница замены и точка вставки откатываются `_trim_trailing_
    noise`, тем же правилом, каким давно живёт `remove_mapping_entry`: без него
    замена уносила висячий комментарий секции вместе с пустой строкой-
    разделителем, а вставка вставала ПОСЛЕ чужого комментария, делая его своей
    шапкой (ревью MR #80, M11)."""
    p_start, p_end, p_indent, p_rest = find_anchor(text, parent_dotted)
    parent_key = parent_dotted.rsplit(".", 1)[-1]
    span_end = section_end(text, p_end, p_indent)
    existing_indent = _child_indent_len(text[p_end:span_end], len(p_indent))
    child_indent = " " * existing_indent if existing_indent is not None else p_indent + "  "
    new_block = "".join(f"{child_indent}{line}\n" if line else "\n" for line in block_lines)
    try:
        k_start, k_end, k_indent, _ = _find_child_anchor(text, parent_dotted, key)
    except KeyError:
        block_end = _exclude_next_attached_comment(text, span_end)
        if not _has_content(text[p_end:block_end]):
            # Родитель без единой записи-строки ("parent:" ИЛИ "parent: {}", в т.ч.
            # с висячими комментами под ним) — нормализуем инлайновую форму в
            # блочную и вставляем первую запись. Непустой инлайновый мэппинг сюда
            # не доедет: `_empty_parent_tail` отказывает. `block_end` здесь и
            # граница, и точка вставки — откатывать его нечем и незачем.
            tail = _empty_parent_tail(p_rest, parent_dotted)
            return (
                text[:p_start]
                + f"{p_indent}{parent_key}:{tail}\n"
                + text[p_end:block_end]
                + new_block
                + text[block_end:]
            )
        return _splice(text, _trim_trailing_noise(text, p_end, block_end), new_block)
    entry_start = _attached_comment_start(text, k_start, k_indent)
    entry_end = _trim_trailing_noise(
        text, k_start, _exclude_next_attached_comment(text, section_end(text, k_end, k_indent))
    )
    return text[:entry_start] + new_block + text[entry_end:]


def _has_content(segment: str) -> bool:
    """Есть ли в фрагменте хоть одна строка с реальным содержимым (не пустая,
    не комментарий) — тем же критерием, что цикл в `section_end`."""
    return any(line.strip() and not line.strip().startswith("#") for line in segment.splitlines())


def _trim_trailing_noise(text: str, start: int, end: int) -> int:
    """`end` диапазона записи, откаченный назад через хвостовые пустые строки и
    комментарии.

    Концом записи считается конец её последней СОДЕРЖАТЕЛЬНОЙ строки: всё, что
    висит следом, ей не принадлежит. `_exclude_next_attached_comment` защищает лишь
    прогон, вплотную примыкающий к следующей строке И с её отступом, — комментарий,
    отделённый пустой строкой, под это правило не попадал и уезжал в удаляемый
    диапазон. Валидация потерю не ловит (результат схемно валиден), бэкапа у записи
    реестра нет. Удалять чужой текст безопаснее «недо-», чем «пере-»."""
    while end > start:
        line_start = text.rfind("\n", start, end - 1) + 1
        if line_start < start:
            break
        line = text[line_start:end]
        if line.strip() == "" or line.strip().startswith("#"):
            end = line_start
            continue
        break
    return end


def remove_mapping_entry(text: str, parent_dotted: str, key: str) -> str:
    """Удалить запись словаря `key` из-под `parent_dotted` (слот/привязка
    «не задан», модель из реестра — свой файл, не оверлей, правится свободно).

    Родителя или записи нет в `text` → идемпотентный no-op (запись и так
    «отсутствует»). Удаление затрагивает СОБСТВЕННЫЙ прикреплённый комментарий
    записи (уходит вместе с ней — иначе повис бы над следующей, искажая
    смысл), но НЕ прикреплённый комментарий СЛЕДУЮЩЕЙ записи/секции (тот ей и
    принадлежит). Удаление ПОСЛЕДНЕЙ записи оставляет родителя ВАЛИДНЫМ
    маппингом (`parent: {}`), а не пустым ключом без значения — иначе YAML
    развернёт его в `None`, и схема (`dict[str, ModelEntry]` и т.п.) упадёт;
    граница этого схлопывания (`block_end`) так же защищена от поглощения
    прикреплённого комментария СЛЕДУЮЩЕГО топ-уровневого ключа/секции (не
    только границы отдельных записей внутри родителя). Висячие комментарии
    внутри секции и трейлинг-комментарий строки родителя схлопывание переживают:
    они описывают секцию, а не удаляемую запись."""
    try:
        k_start, k_end, k_indent, _ = _find_child_anchor(text, parent_dotted, key)
    except KeyError:
        return text
    p_start, p_end, p_indent, p_rest = find_anchor(text, parent_dotted)  # родитель точно есть
    parent_key = parent_dotted.rsplit(".", 1)[-1]
    entry_start = _attached_comment_start(text, k_start, k_indent)
    entry_end = _trim_trailing_noise(
        text, k_start, _exclude_next_attached_comment(text, section_end(text, k_end, k_indent))
    )
    block_end = _exclude_next_attached_comment(text, section_end(text, p_end, p_indent))
    remaining = text[p_end:entry_start] + text[entry_end:block_end]
    if not _has_content(remaining):
        # Схлопывание переписывает СТРОКУ родителя — трейлинг-комментарий на ней
        # описывает секцию, а не удаляемую запись; `remaining` — висячие комменты
        # внутри секции, тоже не её (свой, прикреплённый, уже ушёл в `entry_start`).
        tail = _empty_parent_tail(p_rest, parent_dotted)
        return text[:p_start] + f"{p_indent}{parent_key}: {{}}{tail}\n" + remaining + text[block_end:]
    return text[:entry_start] + text[entry_end:]


def _split_trailing_comment(rest: str) -> tuple[str, str]:
    """Хвост строки ключа → (значение, трейлинг-комментарий с ведущими пробелами
    либо ''). Решётка внутри кавычек — часть значения, а не комментарий; внутри
    двойных кавычек уважается escape `\\`, внутри одинарных — удвоение `''`."""
    i = _trailing_comment_start(rest)
    if i < 0:
        return rest, ""
    j = i
    while j > 0 and rest[j - 1] in " \t":
        j -= 1
    return rest[:j], rest[j:]


def _trailing_comment_start(rest: str) -> int:
    """Индекс решётки, открывающей трейлинг-комментарий вне кавычек, либо -1."""
    quote = None
    i = 0
    while i < len(rest):
        ch = rest[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return i
        i += 1
    return -1


def _value_scalar(value_text: str):
    """Значение строки как YAML-скаляр — тем же правилом, каким `_key_scalar`
    сравнивает ключи: «то же значение» обязано сходиться поверх начертаний
    (старая строка написана рукой в одинарных кавычках, новую выдал `safe_dump`
    без кавычек вовсе). Не разобралось — сравниваем литералом."""
    try:
        return yaml.safe_load(value_text)
    except yaml.YAMLError:
        return value_text.strip()


def carry_entry_comments(text: str, parent_dotted: str, key: str, block_lines: list[str]) -> list[str]:
    """`block_lines` с комментариями СТАРОЙ записи `key` из `text`: прикреплённый
    комментарий записи — первыми строками (ровно тем каналом, который контракт
    `upsert_mapping_entry` и предлагает вызывающему), трейлинг-комментарии —
    обратно на строки, оставшиеся с ТЕМ ЖЕ ключом и ТЕМ ЖЕ значением.

    Нужна вызывающим, которые пересобирают блок записи из разобранного словаря
    (слой записи реестра): reparse+reserialize честно переносит данные, но не
    комментарии — правка одного поля молча уносила и комментарий-шапку записи
    («прод-эндпоинт, не трогать…»), и инлайновые на соседних НЕТРОНУТЫХ строках.
    Валидация потерю не ловит (результат схемно валиден), бэкапа у записи нет.

    Трейлинг ПРАВИМОЙ строки (значение изменилось) намеренно не переносится: он
    описывал старое значение, приписать его новому — соврать; так вела себя и
    построчная правка до пересборки. Ключ и значение сверяются как YAML-скаляры,
    а не побайтово. Записи нет в `text` — block_lines отдаются как есть."""
    try:
        k_start, k_end, k_indent, _rest = _find_child_anchor(text, parent_dotted, key)
    except KeyError:
        return list(block_lines)
    entry_start = _attached_comment_start(text, k_start, k_indent)
    attached = [line[len(k_indent) :].rstrip("\r\n") for _s, _e, line in _lines_with_offsets(text[entry_start:k_start])]
    entry_end = _trim_trailing_noise(
        text, k_start, _exclude_next_attached_comment(text, section_end(text, k_end, k_indent))
    )
    old_comments = _collect_trailing_comments(text[k_start:entry_end])
    out = [_reattach_trailing_comment(line, old_comments) for line in block_lines]
    return attached + out


def _collect_trailing_comments(entry_text: str) -> list[tuple]:
    """[(ключ-скаляр, значение-скаляр, трейлинг-комментарий)] строк записи с комментарием."""
    old_comments: list[tuple] = []
    for _s, _e, line in _lines_with_offsets(entry_text):
        match = _KEY_LINE_RE.match(line)
        if match is None:
            continue
        value, comment = _split_trailing_comment(match.group("rest"))
        if comment.strip():
            old_comments.append((_key_scalar(match.group("key").strip()), _value_scalar(value), comment.rstrip("\r\n")))
    return old_comments


def _reattach_trailing_comment(line: str, old_comments: list[tuple]) -> str:
    """Вернуть строке старый трейлинг, если ключ и значение те же (комментарий расходуется)."""
    match = _KEY_LINE_RE.match(line)
    if match is None:
        return line
    value, own = _split_trailing_comment(match.group("rest"))
    if own:
        return line
    wanted = (_key_scalar(match.group("key").strip()), _value_scalar(value))
    for idx, (old_key, old_value, comment) in enumerate(old_comments):
        if (old_key, old_value) == wanted:
            del old_comments[idx]
            return line + comment
    return line


def entry_uses_yaml_anchors(text: str, parent_dotted: str, key: str) -> bool:
    """Использует ли СУЩЕСТВУЮЩАЯ запись `key` YAML-якоря/alias/merge (`&a`, `*a`,
    `<<: *a`). Вызывающему, который пересобирает блок через reparse+reserialize,
    это вопрос «можно ли вообще»: `safe_load` разворачивает alias и merge в
    обычные ключи, и пересборка молча инлайнит ссылку (или роняет объявление
    якоря) — текст остаётся валидным, потерю не ловит никто.

    Спрашивается ПАРСЕР (поток событий с позициями), а не байты строк: `"*wild"`
    в кавычках — строка, и текстовый детект отбивал бы легальные записи. Ответ —
    по позиции: событие с якорем внутри диапазона записи. Записи нет или текст
    не разбирается — False: негодный YAML отбивает своя проверка вызывающего."""
    try:
        k_start, k_end, k_indent, _rest = _find_child_anchor(text, parent_dotted, key)
    except KeyError:
        return False
    entry_end = section_end(text, k_end, k_indent)
    try:
        for event in yaml.parse(text):
            if getattr(event, "anchor", None) is None:
                continue
            if k_start <= event.start_mark.index < entry_end:
                return True
    except yaml.YAMLError:
        return False
    return False


def write_profile_pointer(path, name: str, *, base_path, extra_candidates=None) -> None:
    """Запись `profile: "<name>"` (пустое имя — деактивация): якорь (топ-уровневый
    `profile:` из пакетного шаблона) и значение собираются В ПАМЯТИ и уходят ОДНОЙ
    валидируемой записью (`write_text`) — тот же принцип, что у записи реестра
    (`config_modules/registry_write.py::_commit`): дописывание якоря — часть той же
    несостоявшейся записи, а не отдельный побочный эффект. Якорь, записанный
    отдельным заходом (`ensure_pointer_anchor`), переживал отказ валидации ниже —
    в файле без ключа `profile` оставался `profile: ""`, а пустой указатель в
    проектном слое побеждает merge по ФАКТУ наличия ключа: действующий профиль
    человека молча гас. Файл, которого ещё нет (проектный `.gc/config.yml`),
    создаёт отдельная простая функция — текстовые примитивы требуют существующего
    файла.

    Дом — здесь, у примитивов указателя, которые она и склеивает: потребителей
    двое (секция «Профили» `/settings` и финал мастера, снимающий указатель на
    ступени `without_profile`), и вторая редакция этой склейки разъехалась бы с
    первой на первой же правке — тем же способом, каким расходились редакции
    резолва указателя. Куда писать, отвечает
    `config_modules.layers.profile_pointer_write_target` (вопрос слоёв, не
    файла) — оба потребителя спрашивают её.

    Валидация — фаза 2 (`validate_config_text_with_sections`), а не синтаксис
    одного файла (тот же принцип, что и у остальных записей реестра,
    `config_modules/registry_write.py::_commit`): указатель кросс-слоен по
    устройству (слот активного профиля вправе жить не в базе), и вердикт по
    одному файлу пропустил бы запись, после которой стек следующего старта не
    соберётся (легальная раскладка «модели в базе, слот в профиле» — снятие
    профиля рвёт единственный слот). `path` бывает и проектным `.gc/config.yml`
    — тогда текст садится в стек ПОДСТАНОВКОЙ на место проектного слоя
    (`target=path`), а не как содержимое базы, иначе вердикт судил бы не тот
    файл, который реально меняется. То же самое — и у ветки создания: файл
    ещё не существует (первая активация проектного профиля), но кандидат на
    его будущее содержимое ровно тот же, что напишет `create_pointer_file`,
    и подстановка на место проектного слоя работает и без файла на диске
    (`_merge_layers` подставляет кандидат ДО чтения, а не после).

    `base_path` — базовый конфиг ЭТОГО запуска: относительно него собирается
    стек, когда пишется не он сам.

    `extra_candidates` — доп. {путь: документ} сверх указателя, тоже
    подставляемые при валидации (`config_modules.layers._merge_layers`,
    приняв несколько кандидатов сразу). Нужны переименованию профиля: указатель
    пишется на НОВОЕ имя ДО физического `rename()` файла профиля, и без будущего
    документа профиля НА ЕГО будущем пути указатель не резолвится вовсе —
    валидация судила бы стек БЕЗ профиля, которого просто ещё нет под этим
    именем.

    Снятие указателя (`name == ""`) идёт с `lenient_prebroken=True`: это дверь
    НАРУЖУ — она хуже не делает никогда, а строгий вход запирал юзера ВНУТРИ,
    если стек сломан ЧУЖИМ слоем (битый managed, висячая ссылка соседнего
    оверлея) — снять свой же указатель, ничего не чинящий в чужой поломке, было
    нечем (ревью MR #80, #2). Активация и переименование остаются строгими:
    их запись меняет САМ СОСТАВ стека — активирует или переносит поломку — и
    поблажка здесь подменяла бы отказ чужой причиной (регресс L19)."""
    from gc_core.config import _default_config_path, validate_config_text_with_sections

    path = Path(path)
    is_base = path.resolve() == Path(base_path).resolve()
    lenient_prebroken = name == ""
    if not path.is_file():
        candidate = f"profile: {quote_scalar(name)}\n"
        if is_base:
            validate_config_text_with_sections(
                candidate, base_path=path, extra_candidates=extra_candidates, lenient_prebroken=lenient_prebroken
            )
        else:
            validate_config_text_with_sections(
                candidate,
                base_path=base_path,
                target=path,
                extra_candidates=extra_candidates,
                lenient_prebroken=lenient_prebroken,
            )
        create_pointer_file(path, name)
        return
    text = read_user_text(path)
    try:
        find_anchor(text, "profile")
    except KeyError:
        text = insert_missing(text, "profile", read_user_text(_default_config_path()))
    try:
        text = set_scalar(text, "profile", name)
    except KeyError:
        raise ValueError("не удалось записать: profile")
    if is_base:

        def validate(candidate: str) -> None:
            validate_config_text_with_sections(
                candidate, base_path=path, extra_candidates=extra_candidates, lenient_prebroken=lenient_prebroken
            )
    else:

        def validate(candidate: str) -> None:
            validate_config_text_with_sections(
                candidate,
                base_path=base_path,
                target=path,
                extra_candidates=extra_candidates,
                lenient_prebroken=lenient_prebroken,
            )

    write_text(path, text, validate=validate)


def create_pointer_file(path, name: str) -> None:
    """Создать НОВЫЙ config.yml с единственной строкой `profile: "<name>"`.

    Единственный случай, когда writer сам создаёт файл (спека §11/§12.3 — активация
    профиля из проекта, где `.gc/config.yml` ещё не существует); `write_values`
    по контракту требует существующего файла."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"profile: {quote_scalar(name)}\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def create_profile_file(path, skeleton: str) -> None:
    """Создать НОВЫЙ файл профиля из скелета (`config_modules.profiles.PROFILE_SKELETON`).

    По образцу `create_pointer_file` (тот же приём атомарной записи), но
    ОТКАЗЫВАЕТ, если файл уже существует — в отличие от указателя config.yml
    (создаётся только когда его заведомо нет), файл профиля пользователь мог
    уже завести вручную, затирать его молча нельзя."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"{path}: файл профиля уже существует")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(skeleton, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def ensure_pointer_anchor(path, template_path, key: str = "profile") -> None:
    """Гарантировать топ-уровневый якорь `key:` (`profile:`/`level:`) в СУЩЕСТВУЮЩЕМ
    config.yml.

    Якорь уже есть (любое значение) — идемпотентный no-op. Иначе вставляет блок
    `<key>: <умолчание>` (с прикреплёнными комментами) из шаблона (`template_path`,
    обычно `config.default.yml`) за блоком соседа по порядку шаблона — тем же
    приёмом, что `insert_missing`. После вставки `write_values`/`set_scalar_at_path`
    находят якорь и правят значение обычным путём.

    Нужен именно топ-уровневым скалярам: их `write_values` сам дописать не может —
    его фоллбэк на шаблон работает только для dotted-ключей (`секция.поле`), а
    конфиги, созданные до появления ключа, этой строки просто не содержат."""
    path = Path(path)
    # Чтение — общим читателем: он переводит ОБА класса «не читается» в `ValueError`
    # с именем файла. Вызывающие (мастер, секция «Режимы») ловят `(ValueError,
    # OSError)` и печатают «✗ не сохранено: …», но `OSError` доезжал без пути, а
    # `UnicodeDecodeError` — голой жалобой кодека: человек видел «'utf-8' codec
    # can't decode byte 0xff» и не знал, какой из конфигов чинить.
    text = read_user_text(path)
    try:
        find_anchor(text, key)
        return
    except KeyError:
        pass
    template_text = read_user_text(template_path)
    new_text = insert_missing(text, key, template_text)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def ensure_section_anchor_text(text: str, section: str, template_text: str) -> str:
    """Текст с гарантированным топ-уровневым якорем `<section>:` (`models`/`slots`/
    `assign`) — обобщение `ensure_pointer_anchor` на произвольный топ-ключ (тот же
    приём, но `template_text` — уже готовая строка, не путь: для config.yml это
    `config.default.yml`, для профиля — скелет либо пустая строка).

    Якорь уже есть — текст возвращается как есть. Иначе:
    - `template_text` несёт `section` НЕЗАКОММЕНТИРОВАННЫМ → блок из шаблона через
      `insert_missing` (с прикреплёнными комментами шаблона), как `ensure_pointer_anchor`;
    - `section` в шаблоне закомментирован (`config.default.yml` документирует
      models/slots/assign закомментированными строками — вставлять из шаблона
      нечего, `find_anchor`/`insert_missing` не видят закомментированный ключ)
      → голый якорь `<section>: {}` с отдельным поясняющим комментарием, на месте
      соседа по порядку шаблона (`_top_level_insert_at` видит и закомментированные
      топ-ключи — иначе секции реестра валились бы в хвост файла).

    Текстовая форма — не только ради вызывающего с путём (`ensure_section_anchor`):
    слой записи реестра собирает весь итоговый текст в памяти и пишет ОДИН раз,
    иначе отвергнутая валидацией запись всё равно оставляла бы в файле якорь."""
    try:
        find_anchor(text, section)
        return text
    except KeyError:
        pass
    try:
        return insert_missing(text, section, template_text)
    except KeyError:
        pass
    block = f"# {section}: добавлено автоматически\n{section}: {{}}\n"
    insert_at = _top_level_insert_at(text, section, template_text)
    if insert_at is None:
        sep = "" if text.endswith("\n") else "\n"
        return text + sep + "\n" + block
    return text[:insert_at] + "\n" + block + text[insert_at:]


def ensure_section_anchor(path, section: str, template_text: str) -> None:
    """`ensure_section_anchor_text` для файла: якорь уже есть — идемпотентный
    no-op (файл не переписывается), иначе атомарная запись текста с якорем."""
    path = Path(path)
    text = read_user_text(path)
    new_text = ensure_section_anchor_text(text, section, template_text)
    if new_text == text:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


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
    voice — фича не включена «из коробки»). KeyError, если блока нет.
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
    out = [lines[i] for i in range(cstart, start)]  # описание — как есть
    out += [_uncomment(lines[i]) for i in range(start, end)]  # шапка + поля — раскомментированы
    return "".join(out)


def _try_set(text: str, key: str, value, setter) -> "str | None":
    try:
        return setter(text, key, value)
    except KeyError:
        return None


def _set_from_template(text: str, key: str, value, setter, template_text: str) -> "str | None":
    """Записать dotted-ключ, которого в тексте нет, опираясь на шаблон; None — не удалось."""
    section = key.split(".")[0]
    try:
        return setter(insert_missing(text, key, template_text), key, value)
    except KeyError:
        pass
    try:
        find_anchor(text, section)
    except KeyError:
        section_alive = False
    else:
        section_alive = True
    try:
        block = uncomment_template_block(template_text, section)
        if section_alive:
            # Секция ЖИВАЯ, но этого под-ключа в ней нет. `insert_missing` выше не
            # нашёл его и в шаблоне — потому что шаблон несёт такие секции
            # закомментированными (voice). Разворачиваем блок шаблона во временный
            # текст и берём строку оттуда: дописать под-ключ в живую секцию можно,
            # а дублировать саму секцию нельзя (см. ветку ниже). Находка ревью:
            # раньше здесь сдавались, и ключ уходил в `missing`.
            return setter(insert_missing(text, key, block), key, value)
        # Секции нет в файле вообще (не только у этого ключа) — можно
        # безопасно развернуть блок шаблона. Если секция уже живая
        # (юзер завёл её сам, частично) — трогать текст нельзя: второй
        # top-level блок задублировал бы ключ, а PyYAML на дублях не
        # ругается и молча берёт последний, затирая юзерские значения.
        # Разворот — в отдельную переменную, а не в `text`: если ключа нет
        # и в блоке шаблона, setter упадёт KeyError уже ПОСЛЕ разворота, и
        # присвоенный `text` дошёл бы до записи. Юзер получил бы включённой
        # секцию, которую сознательно держал закомментированной, — при том
        # что ключ честно отчитан как не записанный.
        sep = "" if text.endswith("\n") else "\n"
        return setter(text + sep + "\n" + block, key, value)
    except KeyError:
        return None


def write_values(path, values: dict, *, template_path=None, validate=None) -> list[str]:
    """Записать {ключ: значение} в YAML-файл path (атомарно).

    Ключ — dotted-путь (path-aware) или legacy-leaf (первое совпадение).
    Отсутствующий якорь при заданном template_path дописывается блоком из
    шаблона. validate(text) бросает ValueError → файл НЕ записывается.
    Возвращает ключи, которые записать не удалось.
    """
    path = Path(path)
    text = read_user_text(path)
    template_text = None
    missing: list[str] = []
    for key, value in values.items():
        setter = set_scalar_at_path if "." in key else set_scalar
        new_text = _try_set(text, key, value, setter)
        if new_text is None and template_path is not None and "." in key:
            if template_text is None:
                template_text = read_user_text(template_path)
            new_text = _set_from_template(text, key, value, setter, template_text)
        if new_text is None:
            missing.append(key)
        else:
            text = new_text
    if missing:
        # Всё или ничего: записать половину значений хуже, чем не записать ничего.
        # Прежде соседи по вызову ложились в файл, а про несохранённые человек узнавал
        # (в /settings) или не узнавал (в мастере) — и на диске оставалось ни то ни сё.
        return missing
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


def write_text(path, text: str, *, validate=None) -> None:
    """Атомарно записать УЖЕ СОБРАННЫЙ текст (сестра write_values — для
    вызывающих, что сами составили итоговый текст через block-level примитивы
    upsert_mapping_entry/remove_mapping_entry, а не через плоские dotted-значения).

    validate(text) бросает ValueError → файл НЕ записывается (тот же контракт,
    что у write_values)."""
    path = Path(path)
    if validate is not None:
        validate(text)  # ValueError → наружу, файл не тронут
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        atomic_replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
