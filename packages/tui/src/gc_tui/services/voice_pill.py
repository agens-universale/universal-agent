"""Рендер «голосовой пилюли» для footer-left: rich.Text шириной PILL_WIDTH ячеек.

Чистые функции (без Textual): поячеечный градиент логотипа (GRADIENT_WARM→MID→COOL),
запись — «● REC» + живой спектр-эквалайзер, распознавание — волна расщепления;
таймер справа. Кадры листает вызывающий (set_interval экрана), сюда приходит
номер кадра. Визуал зафиксирован единый (живая приёмка 2026-07-05), без
конфигурации. Спека: docs/tui/specs/2026-07-05-voice-input-design.md.
"""

from __future__ import annotations

import math

from rich.style import Style
from rich.text import Text

from gc_tui import palette
from gc_tui.palette import TEXT_ON_FILL

PILL_WIDTH = 21
# Рампа уровней (индекс 0 = «пусто», виден только градиент): braille — рисуется
# любым терминальным шрифтом (блок-«восьмушки» ▁▂▃ есть не везде, живой прогон).
_RAMP = " ⡀⣀⣤⣶⣿"
_MAX_LEVEL = len(_RAMP) - 1  # уровни 0..5
_WAVE = (1, 3, 5, 3, 1)  # симметричный «пик» (расщепление, распознавание)
_FLIGHT_CELLS = 7  # путь копии от центра до растворения (ячеек)
_CYCLE_FRAMES = 32  # полный цикл пары (~2.6с при кадре 0.08с)
_WAVE_HALF = len(_WAVE) // 2
_SPARK_MIN = 0.35  # ниже — ячейка гаснет (иначе за волной тянется грязь)


def format_rec_time(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def format_stt_time(seconds: float) -> str:
    # «с» — единица времени как во всех таймингах TUI («Выполнил за 5с»)
    return f"{seconds:.1f}с".replace(".", ",")


def _lerp_hex(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
    r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
    return f"#{round(r1 + (r2 - r1) * t):02x}{round(g1 + (g2 - g1) * t):02x}{round(b1 + (b2 - b1) * t):02x}"


def _gradient_bg(pos: float) -> str:
    """Логотипная лента: GRADIENT_WARM → MID → COOL (pos 0..1)."""
    if pos <= 0.5:
        return _lerp_hex(palette.GRADIENT_WARM, palette.GRADIENT_MID, pos * 2)
    return _lerp_hex(
        palette.GRADIENT_MID, palette.GRADIENT_COOL, (pos - 0.5) * 2
    )


_WAVE_NODES = (0.0,) + tuple(float(v) for v in _WAVE) + (0.0,)  # мягкие концы
_WAVE_CENTER_NODE = _WAVE_HALF + 1


def _wave_at(distance: float) -> float:
    """Высота волны на расстоянии `distance` ячеек от её центра.

    Профиль задан узлами _WAVE, между узлами — линейно. Непрерывность здесь и есть
    плавность полёта: центр копии дробный, и ячейка берёт ту высоту, которая ей
    причитается, вместо «волна целиком перескочила на ячейку вправо»."""
    i = distance + _WAVE_CENTER_NODE
    low = math.floor(i)
    if low >= len(_WAVE_NODES) - 1:
        return 0.0
    frac = i - low
    return _WAVE_NODES[low] + (_WAVE_NODES[low + 1] - _WAVE_NODES[low]) * frac


def _transcribing_levels(frame: int) -> list[int]:
    """Расщепление: копии волны летят из центра к краям, оседая в линию и тая.

    Конвейер: вторая пара стартует на полцикла позже — мёртвых пауз нет.
    В центре пульсирует «зерно»-источник.

    Позиция копии ДРОБНАЯ: волна стоит между ячейками, и яркость делится между
    соседними по весу (растушёвка, как субпиксель в графике). Раньше позиция
    округлялась до целой ячейки, и на 16 кадров приходилось 8 положений — волна не
    летела, а перескакивала раз в треть секунды. Копим яркость непрерывной величиной
    и квантуем по рампе ОДИН раз, в конце: округлять на каждом слагаемом значило бы
    вернуть ту же ступеньку."""
    acc = [0.0] * PILL_WIDTH
    center = PILL_WIDTH // 2
    for phase_shift in (0, _CYCLE_FRAMES // 2):
        p = ((frame + phase_shift) % _CYCLE_FRAMES) / _CYCLE_FRAMES  # 0..1
        if p >= 0.92:
            continue  # копия догорела — следующая уже в пути
        offset = 1.0 + p * _FLIGHT_CELLS
        decay = 1.0 - p * 0.85
        for direction in (-1, 1):
            head = center + direction * offset  # центр копии, ДРОБНЫЙ
            for pos in range(PILL_WIDTH):
                value = _wave_at(abs(pos - head)) * decay
                if value > acc[pos]:
                    acc[pos] = value
    levels = [0] * PILL_WIDTH
    for pos, value in enumerate(acc):
        if value < _SPARK_MIN:
            continue
        levels[pos] = min(_MAX_LEVEL, max(1, round(value)))
    # Зерно-пульс: счёт по ПАРАМ кадров — при кадре 0.08с посимвольная смена читалась
    # бы как дрожание (12 раз в секунду), а не как пульс.
    levels[center] = max(levels[center], (2, 3)[(frame // 2) % 2])
    return levels


_REC_LABEL_START = 2  # отступ метки «● REC» от левого края
_METER_START = _REC_LABEL_START + 6  # спектр-эквалайзер — правее «● REC »
_METER_WIDTH = 7  # ячеек метра (правее — зазор и таймер)
_METER_FLOOR = 0.003  # шумовой гейт: ниже — метр пуст


def _recording_cells(frame: int, bands: tuple) -> list[str]:
    """Запись: «● REC» (кругляшок мигает: 3 кадра виден / 3 скрыт ≈ 0.45с) +
    спектр-эквалайзер реального сигнала: каждый столбец — своя частотная
    полоса (recorder.BAND_FREQS), высота — уровень полосы в дБ-шкале; все
    столбцы «пляшут» одновременно, затухание/АРУ дают recorder и flow."""
    cells = [" "] * PILL_WIDTH
    dot = "●" if (frame // 3) % 2 == 0 else " "
    label = f"{dot} REC"
    for i, ch in enumerate(label):
        pos = _REC_LABEL_START + i
        if pos < PILL_WIDTH:
            cells[pos] = ch
    # дБ-шкала: уровни полос речи малы (~0.01–0.2), линейная шкала оставила бы
    # эквалайзер пустым. −50..0 дБ → 0..1, ниже шумового гейта — тишина.
    for k, band in enumerate(bands[:_METER_WIDTH]):
        pos = _METER_START + k
        if pos >= PILL_WIDTH:
            break
        if band <= _METER_FLOOR:
            continue
        strength = min(1.0, (math.log10(band) + 2.5) / 2.5)
        idx = round(strength * _MAX_LEVEL)
        if idx > 0:
            cells[pos] = _RAMP[idx]
    return cells


def render_pill(
    stage: str, elapsed: float, frame: int, bands: tuple = ()
) -> Text:
    """stage: "recording" | "transcribing" → Text ровно PILL_WIDTH ячеек.

    bands — спектр микрофона по полосам 0..1 (эквалайзер записи)."""
    timer = (
        format_rec_time(elapsed)
        if stage == "recording"
        else format_stt_time(elapsed)
    )
    if stage == "recording":
        cells = _recording_cells(frame, bands)
    else:
        cells = [_RAMP[lvl] for lvl in _transcribing_levels(frame)]
    t_start = PILL_WIDTH - len(timer) - 1  # таймер поверх правого края
    for i, ch in enumerate(timer):
        cells[t_start + i] = ch
    if t_start - 1 >= 0:
        cells[t_start - 1] = " "  # зазор-воздух между анимацией и таймером
    text = Text()
    for i, ch in enumerate(cells):
        text.append(
            ch,
            style=Style(
                color=TEXT_ON_FILL, bgcolor=_gradient_bg(i / (PILL_WIDTH - 1))
            ),
        )
    return text
