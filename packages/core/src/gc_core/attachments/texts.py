"""Тексты про изображения, которые видит МОДЕЛЬ — по-английски, по конвенции
проекта. Единственный источник: проекция, сводка, тул просмотра и «глаза» берут
строки только отсюда. Шаблоны заполняются ``str.format`` именованными полями:
``label`` — ``Attachment.label``, ``wh`` — ``wh_text(att)``, ``path`` —
``Attachment.path``, ``source`` — ``Attachment.source``, ``reason`` — причина сбоя.

Мостовой текст ``(tool results received)`` живёт у моста —
``message_policy/chat_completions.py``.
"""

from gc_core.attachments.model import TOOL_LABEL
from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME

STUB_OUT_OF_CONTEXT = "[{label} · {wh} · not in context · file {path} · view it with fs_view_image]"
# Слепой модели при живых «глазах»: без «другая модель опишет» слабая модель читает
# только «ты не видишь» и ищет обход (OCR, скрипты) вместо тула (смоук 2026-09-29).
STUB_MODEL_BLIND = (
    "[{label} · {wh} · you cannot see this image yourself · call fs_view_image"
    " path={path} — another model will describe it for you]"
)
# Модель запроса известна слепой (проба или `vision: false`): ``alias`` — её
# имя; окончание — по наличию тула просмотра (решение владельца 2026-10-24).
_STUB_NOT_SENT = (
    "[{label} · {wh} · this image was not sent with this request: the"
    " selected model {alias} does not accept images · "
)
STUB_MODEL_REJECTS_VIEW = (
    _STUB_NOT_SENT + "call " + FS_VIEW_IMAGE_NAME + " path={path}"
    " — another model will describe it for you]"
)
STUB_MODEL_REJECTS_NOBODY = (
    _STUB_NOT_SENT + "tell the user that a model with image support is"
    " needed, or ask them to describe it in words]"
)
STUB_UNCHECKED = "[{label} · {wh} · vision not checked · file {path} · fs_view_image can check an available model before viewing; do not use OCR or shell workarounds]"
STUB_PROBE_FAILED = "[{label} · {wh} · image not viewed: vision check {reason} · do not retry viewing or use OCR or shell workarounds in this turn; ask the user to describe it in words]"
STUB_NOBODY = "[{label} · {wh} · not viewed: no available model can work with images — ask the user to describe it in words]"
STUB_FILE_MISSING = "[{label} · file unavailable: {path}]"
# Тот же файл приложен пикселями в другом месте истории — здесь только записка.
STUB_SHOWN_ELSEWHERE = "[{label} · {wh} · the same image is attached elsewhere in this conversation]"
# Подпись перед частью с пикселями — та же форма, что у записок.
STUB_ATTACHED_INLINE = "[{label} · {wh} · attached inline]"

TECH_LINE_TOOL = TOOL_LABEL + ": {source} · {wh}"
TECH_LINE_ASK = "{label} from the user's answer to turn_ask_human · {wh}"

TOOL_ATTACHED = "image attached; you will see it in the next message"
TOOL_ALREADY_IN_CONTEXT = (
    "image already in your context: {label} was sent to you in full with this request; "
    "look at it there"
)
TOOL_NO_VISION = "image not viewed: no available model can work with images"
TOOL_TOO_MANY_IN_STEP = (
    "image not attached: at most {n} images can be viewed in one step; "
    "view the others in the next step"
)
TOOL_NO_IMAGE_STORE = (
    "image not viewed: image storage is unavailable in this run"
)
TOOL_VISION_FAILED = (
    "vision model did not respond ({reason}); you may retry the call"
)

VIEW_DEFAULT_QUESTION = (
    "Describe the image in detail: quote all visible text verbatim, list UI elements, "
    "point out layout defects."
)
EYES_SYSTEM_PROMPT = (
    "You are the vision of a text-only agent. Describe precisely; quote text from the "
    "image verbatim; point out layout defects and inconsistencies; if the question is "
    "narrow, answer it first, then briefly describe the rest."
)

#: Блок картинок в теле сводки; строки внутри — ``STUB_OUT_OF_CONTEXT``.
SUMMARY_IMAGES_OPEN = "<images>"
SUMMARY_IMAGES_CLOSE = "</images>"
