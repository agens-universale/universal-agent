"""Блик по слову: светлое пятно бежит по буквам слева направо, буква за тик.

Центр — пик рампы `shades`, к краям спадает до её первого оттенка. Проход
начинается и кончается за краями слова. Рампа — параметр: индикатор «думаю…»
и шапка стартового экрана красят блик каждый своей.
"""

from __future__ import annotations

from collections.abc import Sequence
from itertools import groupby
from operator import itemgetter

from rich.text import Text


def shimmer_pass(word: str, shades: Sequence[str]) -> int:
    """Шагов прохода блика по слову — от входа слева до ухода за правый край."""
    return len(word) + 2 * (len(shades) - 1)


def shimmer_text(word: str, step: int, shades: Sequence[str]) -> Text:
    """Слово с бликом на шаге прохода `step`; шаг 0 и шаги после прохода —
    блик за краями слова, слово ровным первым оттенком рампы."""
    spread = len(shades) - 1
    center = step - spread
    colors = [
        shades[max(0, spread - abs(i - center))] for i in range(len(word))
    ]
    # Соседние буквы одного оттенка — одним куском: вне блика слово
    # рисуется так же, как без него.
    text = Text()
    for shade, run in groupby(zip(word, colors), key=itemgetter(1)):
        text.append("".join(char for char, _ in run), style=shade)
    return text
