"""Клик-модель строк трейса — единая точка контракта взаимодействия.

Обычный клик строку НЕ выделяет (решение владельца 2026-11-01: выделение на
каждый клик было навязчивым) — он уходит экрану, тот отдаёт фокус ленте.
Клик с модификатором — Ctrl (на Mac — Cmd: терминал передаёт его тем же
битом, что Alt; Ctrl+клик там — правый клик) — выделяет строку уже на нажатии
и открывает экран события на отпускании. Alt — запасной там, где Ctrl+клик
забирает терминал. С клавиатуры: Tab выделяет, Enter
открывает. Двойной клик события не открывает. Клик по СЛУЖЕБНОМУ элементу блока (кнопка копирования,
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

Исключение — картинка внутри строки (плашка реплики, её строка под репликой): она
ведёт себя как ссылка, как кнопка «скопировать» у блока кода, — обычный клик по ней
открывает файл (`_plain_click_image_path`), под тем же гейтом.
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


from gc_tui.messages import BlockToggle, OpenImageRequest


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


def opens_event(event: events.MouseEvent) -> bool:
    """Клик с модификатором открытия: Ctrl или `meta` — так приходят Cmd
    (Mac) и Alt."""
    return event.ctrl or event.meta


class ClickModelMixin:
    """Обычный клик строку не выделяет; клик с Ctrl (на Mac — Cmd) выделяет
    на нажатии и открывает на отпускании (как Tab + Enter).

    Наследник реализует ``_open_request()`` — Message открытия своего экрана,
    или None, если строке нечего открывать (тогда клик с модификатором только
    выделяет). Наследник со своим контрактом (SummaryMarker) переопределяет
    ``on_click``/``on_key`` целиком.
    """

    # Обычное нажатие строку не выделяет: экран фокусирует на MouseDown только
    # того, кто согласен (`focus_on_click`). Нажатие с модификатором выделяет
    # само (`on_mouse_down`) — выделение видно до того, как экран события
    # накроет ленту. Блоки со служебными элементами (BlockPanel, DiffBlock)
    # тем более не берут фокус нажатием по ❐ и по хвосту; за нефокусируемый
    # блок флаг предъявляет экран (`focus_target_at`). focus() зовётся с
    # scroll_visible=False: кликнутая строка и так под курсором.
    FOCUS_ON_CLICK = False

    def _open_request(self) -> Optional[Message]:
        raise NotImplementedError

    def _image_path(self, image_id: str) -> Optional[str]:
        """Файл картинки по `image_id` из meta спана под курсором (плашка или
        подстрока вложения). У строк без картинок — None."""
        return None

    def _plain_click_image_path(self, event: events.Click) -> Optional[str]:
        """Файл картинки, если обычный клик — клик по ней как по ссылке (строки с
        картинками решают, засчитан ли жест). У строк без картинок — None."""
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if opens_event(event) and self.can_focus:
            self.focus(scroll_visible=False)

    def on_click(self, event: events.Click) -> None:
        # Мышиная половина того же гейта, что и Enter в on_key: пока висит
        # security-confirm, клик с модификатором не открывает detail/viewer —
        # иначе экран события накрывает баннер, [Y] уходит в него, а агент
        # ждёт ответа, которого юзер уже не видит. Тогда клик только выделяет.
        if opens_event(event) and not confirm_is_active(self):
            # Картинка — по месту клика (дизайн картинок §20.2): Screen кладёт стиль
            # ячейки под курсором в event.style, спан плашки/подстроки несёт image_id.
            path = self._image_path(event.style.meta.get("image_id") or "")
            message = OpenImageRequest(path) if path else self._open_request()
            if message is not None:
                event.stop()
                if self.can_focus:
                    # Ctrl+клик тоже выделяет: после выхода из открытого экрана
                    # фокус остаётся на этой строке, а не на прошлом выделении.
                    self.focus(scroll_visible=False)
                self.post_message(message)
                return
        if not opens_event(event) and not confirm_is_active(self):
            path = self._plain_click_image_path(event)
            if path:
                event.stop()
                self.post_message(OpenImageRequest(path))
                return
        if opens_event(event) and self.can_focus:
            event.stop()  # не отдать фокус контейнеру #trace (main.py.on_click)
            self.focus(scroll_visible=False)

    def on_key(self, event: events.Key) -> None:
        if event.key == "space":
            # Пробел на строке блока вызовов — раскрыть / свернуть свой блок
            # (спека P4 §5); у строки вне блоков пробел не обрабатывается.
            block_key = getattr(self, "block_key", None)
            if block_key is not None:
                event.stop()
                event.prevent_default()
                self.post_message(BlockToggle(*block_key))
            return
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
