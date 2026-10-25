"""Реестр текстов слота футера главного экрана (строка 2) и минимальная
ширина футера. Технический слой без Textual: тут только тексты и константы;
когда и на сколько показывать — решает экран."""

from __future__ import annotations

from gc_core.attachments.normalize import REASON_NOT_AN_IMAGE, REASON_TOO_LARGE

from gc_tui.services import image_paste

# Минимальная ширина главного футера (как у футеров форм).
MAIN_FOOTER_MIN_COLS = 64

# Подтверждения Esc: второй Esc выполняет действие.
HINT_TEXT = {
    "clear": "Esc ещё раз, чтобы очистить",
    "stop": "Esc ещё раз, чтобы остановить",
    "paste": "Esc ещё раз, чтобы отменить вставку",
}
PASTE_HINT_TEXT = "вставьте ещё раз, чтобы развернуть"

# Вставка изображения: «Вставка…» держится до результата.
IMAGE_PASTE_BUSY_HINT = "Вставка…"
IMAGE_PASTE_WAIT_HINT = "дождитесь окончания вставки"
IMAGE_OPEN_TIP = "Ctrl+клик по плашке — открыть изображение"
IMAGE_REFUSAL_EMPTY_CLIPBOARD = "✗ в буфере обмена нет изображения"
IMAGES_WITH_COMMAND_REFUSAL = (
    "✗ изображения нельзя отправить вместе с командой"
)
# Подсказки самой вставки: их (и только их) снимает конец вставки.
IMAGE_PASTE_OWN_HINTS = (
    IMAGE_PASTE_BUSY_HINT,
    IMAGE_PASTE_WAIT_HINT,
    HINT_TEXT["paste"],
)
# Отказы вставки по причине `image_paste.PasteRefusal`; `{n}` —
# `images.max_per_message`.
IMAGE_PASTE_REFUSALS = {
    image_paste.REFUSE_LIMIT: "✗ не больше {n} изображений в одном сообщении",
    REASON_NOT_AN_IMAGE: "✗ это не изображение",
    REASON_TOO_LARGE: "✗ изображение слишком большое",
    image_paste.REFUSE_NO_TOOL: (
        "✗ для вставки изображений нужен wl-paste или xclip"
    ),
    image_paste.REFUSE_SAVE_FAILED: "✗ не удалось сохранить изображение",
    image_paste.REFUSE_TIMEOUT: (
        "✗ вставка не удалась: истекло время ожидания"
    ),
}

# ↑ в пустом поле: реплику из очереди ядро уже впустило (спека P3 §2.4).
QUEUE_ADMITTED_HINT = "сообщение уже у агента — изменить нельзя"

RETENTION_DETAIL_HINT = "детали вне окна retention; /export или архив"
NESTED_NO_DETAIL = "нет args/output для этого nested-тула"

# /copy: отказы (WARN) и успех (✓ цветом OK); `{n}` — запрошенный номер,
# `{total}` — число ответов агента в сессии.
COPY_REFUSAL_EMPTY = "/copy: в этой сессии ещё нет ответов агента"
COPY_REFUSAL_TOO_BIG = "/copy {n}: ответов агента в сессии {total}"
COPY_REFUSAL_BAD_N = "/copy N — N-й ответ с конца, например /copy 2"
COPY_REFUSAL_FAILED = "/copy: не удалось записать в буфер обмена"
COPY_DONE_LAST = "✓ скопирован последний ответ"
COPY_DONE_NTH = "✓ скопирован ответ {n} из {total}"

# Выбор модели, которая уже работает (спека model-knob §6.4); `{name}` —
# модель.
ALREADY_WORKING_HINT = "{name} уже работает — смены нет"

# Тексты, которые по спеке могут обрезаться многоточием (отказы с
# подстановкой и длинные пояснения); остальные обязаны влезать в
# `MAIN_FOOTER_MIN_COLS - 20`.
TRUNCATABLE = frozenset(
    {
        *IMAGE_PASTE_REFUSALS.values(),
        IMAGES_WITH_COMMAND_REFUSAL,
        RETENTION_DETAIL_HINT,
        COPY_REFUSAL_BAD_N,
        ALREADY_WORKING_HINT,
    }
)


def toggle_info_line(name: str, value: str) -> str:
    """Справка ручки, набранной текстом (`/gate` + Enter): значение и как
    его менять. Ничего не обещает про Enter."""
    return f"/{name}: {value} · ←/→ в меню команд — изменить"
