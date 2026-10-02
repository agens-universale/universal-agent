"""Клик-модель строк трейса — единая точка контракта взаимодействия.

Мышь и клавиатура симметричны: {клик ↔ Tab} выделение, {Ctrl+клик ↔ Enter}
выделение + открытие экрана события. Двойной клик события не открывает
(заменён Ctrl+кликом). Клик по СЛУЖЕБНОМУ элементу блока (кнопка копирования,
хвост-тогл свёртки в обоих состояниях — «… ещё N строк» и «свернуть») блок не
выделяет: он только делает своё действие. Меняешь модификатор или порядок
действий — меняешь здесь, а не в пяти виджетах (TraceRow, ToolCallRow,
UserReplyContainer, BlockPanel, DiffBlock).

DiffBlock конвенцию НЕ нарушает: он не фокусируется вовсе (can_focus=False,
FOCUS_ON_CLICK=False). Событие трейса — это вызов инструмента, а дифф лишь его
часть, поэтому Tab останавливается на строке тула и следующим шагом уходит на
следующее событие; отдельного стопа на диффе нет (живая правка 2026-08-11 —
раньше он был ради Enter-тогла и сбивал ритм обхода). Свёртку переключает
только клик по хвосту; клавиатурного пути к ней нет. Блок переопределяет
on_click целиком (со своим prevent_default(), иначе миксин отработал бы по MRO
следом и выделял бы блок за клик по служебному элементу), а Ctrl+клик отдаёт
миксину — открытие события работает и с нефокусируемого блока.

Чтобы клик по нефокусируемому блоку не двигал фокус ЧУЖОЙ, экран трейса
подменяет поиск цели фокуса на ``focus_target_at`` ниже — см. её докстринг.

Симметрия распространяется и на гейт security-confirm: экран события не
открывает НИ Enter, НИ Ctrl+клик, пока баннер ждёт ответа (`confirm_is_active`).
"""

from __future__ import annotations

from typing import Optional

from textual import events
from textual.message import Message
from textual.widget import Widget


def focus_target_at(
    screen, x: int, y: int, default: Optional[Widget]
) -> Optional[Widget]:
    """Кого Textual сфокусирует нажатием в точке (x, y) — с учётом отказников.

    Textual на MouseDown ищет фокусируемого ПРЕДКА нажатого виджета
    (``Screen.get_focusable_widget_at``: обход ``ancestors_with_self`` по
    ``node.focusable``) и только у НАЙДЕННОГО спрашивает ``focus_on_click()``.
    Свой ``FOCUS_ON_CLICK = False`` нефокусируемый виджет этим поиском не
    предъявляет: обход проходит сквозь него дальше, находит контейнер ленты
    ``#trace`` — и фокус уезжает туда (живой замер 2026-08-11: после клика по
    диффу Tab начинал обход ленты заново вместо следующей строки тула).

    Правило здесь общее и ровно про то, что флаг и обещает: поиск обрывается на
    первом узле, который фокус по клику не принимает. Textual спросит
    ``focus_on_click()`` уже у него, получит False и фокус не тронет вовсе.
    Ни MouseDown глушить, ни делать блок фокусируемым (он вне Tab-обхода
    намеренно) для этого не нужно.

    ``default`` — что вернула бы реализация Textual: её и отдаём, если по дороге
    отказников нет. Подключается экраном, у которого такие блоки есть
    (``TraceScreen.get_focusable_widget_at``).
    """
    try:
        widget, _region = screen.get_widget_at(x, y)
    except Exception:
        return default
    for node in widget.ancestors_with_self:
        if node is default:
            return default
        if isinstance(node, Widget) and not node.focus_on_click():
            return node
    return default


def confirm_is_active(widget) -> bool:
    """Висит ли на экране security-confirm, ждущий ответа юзера.

    Общий предикат для обеих половин клик-модели: пока баннер не отвечен,
    строка не открывает свой экран — иначе он накрывает баннер, тот перестаёт
    получать клавиши, а агент продолжает ждать ответа, которого уже не видно.
    """
    try:
        return bool(getattr(widget.screen, "_confirm_active", False))
    except Exception:
        return False


class ClickModelMixin:
    """Клик = выделить строку (как Tab); Ctrl+клик = выделить и открыть (как Enter).

    Наследник реализует ``_open_request()`` — Message открытия своего экрана,
    или None, если строке нечего открывать (тогда Ctrl+клик ведёт себя как
    обычный клик). Наследник со своим контрактом (SummaryMarker) переопределяет
    ``on_click``/``on_key`` целиком.
    """

    # У СТРОК FOCUS_ON_CLICK намеренно НЕ переопределён (наследуется True):
    # Textual фокусирует строку уже на MouseDown (screen.py, set_focus при
    # нажатии) — выделение видно в момент нажатия, в т.ч. при Ctrl+клике ДО
    # того, как экран события накроет трейс. self.focus() в on_click остаётся
    # как идемпотентная страховка (синтетические клики в тестах идут мимо
    # mousedown-пути экрана), а event.stop() — защита от перехвата фокуса
    # контейнером #trace (main.py.on_click) — живёт на Click-уровне.  # noqa: ERA001  # описание формата, не выключенный код
    #
    # БЛОКИ со служебными элементами (BlockPanel, DiffBlock) ставят
    # FOCUS_ON_CLICK = False: экран фокусирует блок ЛЮБЫМ нажатием, в том числе
    # по кнопке ❐ и по хвосту, и блок подсвечивался бы выделенным там, где
    # выделять нечего. Сам по себе флаг работает только у ФОКУСИРУЕМОГО блока
    # (BlockPanel) — за нефокусируемый (DiffBlock) его предъявляет экран, см.
    # `focus_target_at`. У них носитель выделения — focus() ниже, поэтому он и
    # зовётся с scroll_visible=False: кликнутый блок и так под курсором, а
    # доводка длинного диффа в центр экрана дёргала бы трейс под рукой.

    def _open_request(self) -> Optional[Message]:
        raise NotImplementedError

    def on_click(self, event: events.Click) -> None:
        # Мышиная половина того же гейта, что и Enter в on_key: пока висит
        # security-confirm, Ctrl+клик не открывает detail/viewer — иначе экран
        # события накрывает баннер, [Y] уходит в него, а агент ждёт ответа,
        # которого юзер уже не видит. Ctrl+клик при этом ведёт себя как обычный
        # клик (просто выделяет строку) — обычный клик не трогаем.
        if event.ctrl and not confirm_is_active(self):
            message = self._open_request()
            if message is not None:
                event.stop()
                if self.can_focus:
                    # Ctrl+клик тоже выделяет: после выхода из открытого экрана
                    # фокус остаётся на этой строке, а не на прошлом выделении.
                    self.focus(scroll_visible=False)
                self.post_message(message)
                return
        if self.can_focus:
            event.stop()  # не отдать фокус контейнеру #trace (main.py.on_click)
            self.focus(scroll_visible=False)

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            # Confirm-баннер перехватывает Enter на Screen, но focused-строка
            # получает Key раньше (bubble). Не открывать detail/viewer, пока
            # висит security-confirm — иначе [Y]/Enter открывает tool detail.
            if confirm_is_active(self):
                return
            message = self._open_request()
            if message is not None:
                event.stop()
                self.post_message(message)
