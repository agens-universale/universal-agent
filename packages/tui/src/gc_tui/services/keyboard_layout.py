"""Перевод набранного между раскладками ЙЦУКЕН и QWERTY.

Слово, набранное не в той раскладке (`ьщвуд` вместо `model`, `c;fnm`
вместо `сжать`), переводится по физическим клавишам: каждый символ —
в символ той же клавиши другой раскладки. Слэш не переводится: команда
начинается с `/` в любой раскладке.
"""

from __future__ import annotations

# Клавиши в одном порядке: строчные, затем с Shift.
_LATIN = "`qwertyuiop[]asdfghjkl;'zxcvbnm,.~QWERTYUIOP{}ASDFGHJKL:\"ZXCVBNM<>"
_RUSSIAN = "ёйцукенгшщзхъфывапролджэячсмитьбюЁЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ"
_SWAP = str.maketrans(_LATIN + _RUSSIAN, _RUSSIAN + _LATIN)


def swap_layout(text: str) -> str:
    """`text`, набранный в другой раскладке; символы без пары — как есть."""
    return text.translate(_SWAP)
