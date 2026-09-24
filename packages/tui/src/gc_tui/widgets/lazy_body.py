"""Полотно экранов правок, которое собирается по мере прокрутки (Р23).

Тело диффа дорого не расчётом, а РАСКЛАДКОЙ: готовый `rich.Text` Textual
обязан перевести в свой `Content`, измерить и разложить, и на файле в 4 тыс.
строк, переписанном целиком, эти восемь десятков участков съедали больше
секунды — при том что в кадр помещается один. Поэтому участки монтируются
ПУСТЫМИ, с заранее зарезервированной высотой, а содержимое получают те, что
попали в кадр, плюс запас сверху и снизу; уехавшие далеко — отдают его обратно.

**Высота участка известна до отрисовки** — это число его строк (тело диффа не
переносится, `text-wrap: nowrap`), — поэтому полоса прокрутки честна с первого
кадра, `scroll_visible` доводит кадр до нужного места ещё пустого участка, а
наполнение ничего не двигает: высота виджета от него не меняется.

Служебные строки (заголовок файла, нота, предупреждение) живут наполненными
всегда: их единицы, стоят они гроши, а высота у них своя — от ширины окна и
переноса. Полотну не приходится её угадывать, и от переноса длинного
предупреждения ничего не разъезжается.

Механизм ОДИН на оба экрана правок — вьюер файла и подтверждение отката: тело
у них одной породы, и вторая копия этой арифметики разъехалась бы с первой.
"""
from __future__ import annotations

from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

# Запас вокруг кадра — в его высотах. С ИЗБЫТКОМ осознанно: `PgDn` уводит кадр
# ровно на высоту экрана за нажатие, и запаса в четыре высоты хватает, чтобы при
# зажатой клавише сборка шла впереди глаза, а не догоняла его. Цена запаса
# линейна и мала: на кадре в 38 строк это ~300 строк содержимого, доли
# миллисекунды на участок.
OVERSCAN_SCREENS = 4
# Нижняя граница запаса в строках: на крошечном окне (высота 6) четырёх высот
# не хватило бы и на один участок.
OVERSCAN_MIN_LINES = 150
# Во сколько раз окно удержания больше окна сборки. Гистерезис обязателен: без
# него участок на самой границе отдавал бы содержимое и получал его назад на
# каждый шаг колеса.
KEEP_FACTOR = 3
# Высота служебной строки, ПОКА раскладки ещё нет (сама строка плюс `margin-top`
# у `.line-meta`). Догадка нужна ровно на первый кадр — до того, как Textual
# разложил детей; дальше высоты берутся замером, и ошибка догадки ни на что не
# влияет, кроме числа участков в первом окне.
META_LINES = 2
# Высота кадра, пока её не измерили (экран ещё не разложен) — та же страховка,
# что у ширины строки списка.
FALLBACK_VIEWPORT = 40


class BodySlot(NamedTuple):
    """Место в полотне: виджет и то, чем он наполнится, когда доедет кадр.

    `render is None` — участок служебный: он живёт наполненным всегда и высотой
    распоряжается сам. Иначе `lines` — высота участка тела в строках; её и
    резервирует полотно под пустым виджетом.
    """

    widget: Static
    lines: int = 0
    render: Optional[Callable[[], Text]] = None


class LazyBody(VerticalScroll):
    """`VerticalScroll`, наполняющий свои участки по мере попадания в кадр."""

    # Прокрутка ВБОК: строка тела не переносится (на этом стоит вся ленивая
    # сборка — высота участка известна до отрисовки), поэтому хвост длинной
    # строки достаётся прокруткой, а не переносом. Правило даём типу виджета, а
    # не `#trace`: тот же id носит лента главного экрана, где перенос свой.
    #
    # Размах прокрутки уточняется по мере наполнения участков: у пустого длина
    # самой длинной строки неизвестна. Это мягче вертикального случая — меняется
    # дальность хода, а содержимое под курсором не прыгает.
    DEFAULT_CSS = """
    LazyBody { overflow-x: auto; }
    """

    def __init__(self, *children, **kwargs) -> None:
        super().__init__(*children, **kwargs)
        self._slots: List[BodySlot] = []
        self._at: Dict[int, int] = {}       # id(виджета) → номер участка
        self._built: Set[int] = set()
        self._places: Optional[List[Tuple[float, float]]] = None

    # ---- наполнение ----------------------------------------------------

    def set_slots(self, slots: Sequence[BodySlot]) -> None:
        """Заменить полотно целиком: смонтировать места и собрать первое окно.

        Монтируются ОДНОЙ пачкой: у каждого `mount` своя регистрация в
        приложении, и на полусотне участков поштучный монтаж стоит заметно
        дороже.
        """
        self.remove_children()
        self._slots = list(slots)
        self._at = {id(slot.widget): index for index, slot in enumerate(self._slots)}
        self._built = set()
        self._places = None
        for slot in self._slots:
            if slot.render is not None:
                # Место резервируется ДО отрисовки — тем и держится честная
                # полоса прокрутки. Высота явная и после наполнения: тело диффа
                # не переносится, поэтому строк в нём ровно столько же.
                slot.widget.styles.height = max(1, slot.lines)
        self.mount_all([slot.widget for slot in self._slots])
        # Раскладки на этом такте ещё нет, и первое окно считается по
        # объявленным высотам — чтобы кадр был наполнен сразу, а не через такт;
        # после раскладки окно уточняется замером.
        self._sync()
        self.call_after_refresh(self._sync)

    def ensure(self, widget: Static) -> None:
        """Собрать участок и его окрестность — ДО того, как кадр к нему поедет.

        Прыжок курсора (`n`/`p`) обязан показать содержимое, а не пустое место:
        прокрутка сама наполнит участок лишь следующим тактом. Окрестность
        берётся такая же, как у кадра, вставшего на этот участок сверху.
        """
        index = self._at.get(id(widget))
        if index is None:
            return
        top = self._spans()[index][0]
        reserve = self._reserve()
        self._fill(top - reserve, top + self._viewport() + reserve)

    # ---- окно ----------------------------------------------------------

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._sync()

    def on_resize(self, event) -> None:
        # Сменилась высота кадра — сменилось и окно сборки, а вместе с шириной
        # могли поехать и места (служебная строка переносится). Одноимённый
        # хэндлер родителя Textual зовёт сам (по всему MRO), гасить его нельзя.
        self._places = None
        self._sync()

    def _sync(self) -> None:
        """Наполнить участки в окне и отпустить уехавшие далеко за него."""
        if not self._slots:
            return
        viewport = self._viewport()
        reserve = self._reserve()
        top = self.scroll_y
        self._fill(
            top - reserve,
            top + viewport + reserve,
            keep=(top - reserve * KEEP_FACTOR,
                  top + viewport + reserve * KEEP_FACTOR),
        )

    def _fill(
        self,
        top: float,
        bottom: float,
        *,
        keep: Optional[Tuple[float, float]] = None,
    ) -> None:
        spans = self._spans()
        for index, slot in enumerate(self._slots):
            if slot.render is None:
                continue
            begin, end = spans[index]
            if end >= top and begin <= bottom:
                self._build(index)
            elif keep is not None and (end < keep[0] or begin > keep[1]):
                self._release(index)

    def _build(self, index: int) -> None:
        if index in self._built:
            return
        slot = self._slots[index]
        self._built.add(index)
        slot.widget.update(slot.render())

    def _release(self, index: int) -> None:
        """Отпустить содержимое далёкого участка — место под ним остаётся.

        Пока на экране живо выделение мышью, не отпускаем ничего: протяжка с
        автопрокруткой набирает виджеты, уехавшие из кадра, и опустошённый
        участок вырезал бы из копии свой кусок текста.
        """
        if index not in self._built:
            return
        try:
            if self.screen.selections:
                return
        except Exception:                # экран уже снят — отпускать безопасно
            pass
        self._built.discard(index)
        self._slots[index].widget.update("")

    # ---- метрика -------------------------------------------------------

    def _spans(self) -> List[Tuple[float, float]]:
        """Границы участков в строках полотна: `(верх, низ)` на каждый.

        Берутся ЗАМЕРОМ (`virtual_region` — позиция ребёнка внутри полотна,
        независимая от прокрутки): так учтены и отступы служебных строк, и
        перенос длинного предупреждения. Пока раскладки нет, высоты считаются
        по объявленным — это ровно первый кадр после монтажа.

        Замер КЭШИРУЕТСЯ до следующей смены размера или набора: он идёт через
        карту композитора, а та после прокрутки собирается заново — по всему
        дереву. Прокрутка мест не двигает (высоты участков явные, и наполнение
        их не меняет), поэтому пересчитывать нечего. Замер на прокрутке в 20
        страниц по файлу с 200 правками: 1340 мс внутри синхронизации без кэша
        против 370 мс с ним (1.79 мс на вызов против 0.51).
        """
        if self._places is not None:
            return self._places
        spans: List[Tuple[float, float]] = []
        measured = False
        cursor = 0
        for slot in self._slots:
            region = slot.widget.virtual_region
            if region.height:
                top, height = region.y, region.height
                measured = True
            else:
                top, height = cursor, (slot.lines if slot.render is not None
                                       else META_LINES)
            spans.append((top, top + height))
            cursor = top + height
        if measured:                    # до раскладки запоминать нечего
            self._places = spans
        return spans

    def _viewport(self) -> int:
        height = self.scrollable_content_region.height or self.size.height
        return height if height > 0 else FALLBACK_VIEWPORT

    def _reserve(self) -> int:
        return max(self._viewport() * OVERSCAN_SCREENS, OVERSCAN_MIN_LINES)
