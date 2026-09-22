import re


def glob_to_regex(glob_pattern: str) -> str:
    """
    Convert a glob pattern to a regular expression.
    Supports:
        *       -> any sequence of characters except path separator (/) - zero or more
        **      -> any sequence including path separators - zero or more
        ?       -> any single character except path separator
        [seq]   -> character class
        [!seq]  -> negated character class
    """
    i = 0
    n = len(glob_pattern)
    regex = ""
    in_char_class = False

    while i < n:
        c = glob_pattern[i]

        if c == "[":
            fragment, i, terminated = _parse_char_class(glob_pattern, i)
            regex += fragment
            in_char_class = not terminated
        elif c == "*" and not in_char_class:
            regex, i = _parse_star(glob_pattern, regex, i)
        elif c == "?" and not in_char_class:
            regex += "."
            i += 1
        elif in_char_class:
            regex += c
            i += 1
        else:
            regex += re.escape(c)
            i += 1

    return regex


def _parse_char_class(glob_pattern: str, i: int) -> tuple[str, int, bool]:
    r"""Разбор ``[...]``/``[!...]`` с позиции ``i`` (``glob_pattern[i] == '['``).

    Возвращает (фрагмент regex, следующий индекс, закрыт ли класс).
    Незакрытый класс даёт литерал ``\[`` и сдвиг на один символ.
    """
    n = len(glob_pattern)
    j = i
    is_neglect = j + 1 < n and glob_pattern[j + 1] == "!"
    if is_neglect:
        j += 1
    j += 1
    while j < n and glob_pattern[j] != "]":
        j += 1
    if j >= n:
        # Unterminated, treat as literal
        return re.escape(glob_pattern[i]), i + 1, False
    if is_neglect:
        return "[^" + glob_pattern[i + 2 : j + 1], j + 1, True
    return glob_pattern[i : j + 1], j + 1, True


def _parse_star(glob_pattern: str, regex: str, i: int) -> tuple[str, int]:
    """Разбор ``*``/``**`` с позиции ``i``. Возвращает (новый regex, следующий индекс)."""
    n = len(glob_pattern)
    if not (i + 1 < n and glob_pattern[i + 1] == "*"):
        # Single *
        return regex + "[^/]*", i + 1
    # Handle **
    if i + 2 < n and glob_pattern[i + 2] == "/":
        # **/ -> match any subdirectory depth
        return regex + "(?:.+/)?", i + 3
    if (i + 2) >= n and glob_pattern[i - 1] == "/":
        # end **
        return regex[: i - 1] + ".*", i + 2
    if i > 0 and glob_pattern[i - 1] == "/":
        # /** at start or middle
        return regex + "(?:/.+)?", i + 2
    # Standalone **
    return regex + ".*", i + 2


def compile_glob(glob_pattern: str) -> re.Pattern:
    regex_str = glob_to_regex(glob_pattern)
    return re.compile(f"^{regex_str}$")
