"""Вложения-картинки на проводе (спека §5, §6): какие картинки уходят модели
пикселями и в какой форме уходит сообщение с вложениями.

Чистые функции без роутера и контроллера: «кто смотрит» (``Audience``), живые
id, режим API и чтение байт считает контроллер (``processing_responses``) на
каждый запрос и передаёт значениями.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from gc_core.attachments.model import (
    LIVE_USER_TURNS,
    MAX_REQUEST_IMAGE_BYTES,
    ORIGIN_TOOL,
    ORIGIN_USER,
    Attachment,
    attachments_of,
    wh_text,
)
from gc_core.attachments.texts import (
    STUB_FILE_MISSING,
    STUB_MODEL_BLIND,
    STUB_MODEL_REJECTS_NOBODY,
    STUB_MODEL_REJECTS_VIEW,
    STUB_NOBODY,
    STUB_OUT_OF_CONTEXT,
    STUB_PROBE_FAILED,
    STUB_SHOWN_ELSEWHERE,
    STUB_UNCHECKED,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    user_content_to_text,
)
from gc_core.utils.data_urls import data_url

if TYPE_CHECKING:
    from gc_core.config_modules.images import ImagesConfig
    from gc_core.vision.audience import Audience

#: Единственный режим API, где картинки уходят пикселями (спека §20.5).
_CHAT_COMPLETIONS = "chat_completions"


def compute_live_ids(
    messages: Sequence[Mapping[str, Any]],
    *,
    images: ImagesConfig,
    max_images: int | None = None,
) -> frozenset[str]:
    """Id вложений, которые уходят модели пикселями на этот запрос (спека §6).

    ``messages`` — слитая последовательность запроса (``state.messages`` и батч);
    сообщение, повторённое по ``id``, считается один раз — по первому вхождению.
    Граница хода — реальная реплика человека (``extract_real_user_message_text``
    непуст), в том числе посреди запуска; технические сообщения и ответы тулов
    ход не открывают. Живы: ваши картинки — не больше ``images.live_user_images``
    самых свежих за последние ``LIVE_USER_TURNS`` ходов (текущий включён), снимки
    агента — не больше ``images.live_tool_images`` самых свежих за текущий ход;
    снимок, после которого модель ещё не отвечала (ни текста, ни вызова), жив и
    за границей хода — реплика посреди запуска, дописанная к батчу «ответы тулов
    + картинка», не отнимает у модели обещанное тулом «you will see it».
    Свежесть — позиция: более позднее сообщение, внутри сообщения — более позднее
    вложение. Один файл (один ``id``) в нескольких сообщениях — одна штука.

    Затем бюджет запроса: сумма base64 живых картинок — не больше
    ``MAX_REQUEST_IMAGE_BYTES``; не влезло — живость теряют самые старые, внутри
    сообщения — с первого вложения. Живая картинка уходит пикселями один раз — в
    последнем вхождении (``project_outbound``), поэтому и байты, и возраст
    считаются по нему.

    Последним — потолок модели ``max_images`` (выучен по отказу бэкенда,
    ``vision.rejects``): картинок на проводе не больше него; лишние снимаются так
    же, с самых старых. ``None`` — потолка нет.
    """
    ordered = _unique_by_id(messages)
    parsed = [attachments_of(message) for message in ordered]
    candidates = _live_candidates(ordered, parsed, images)
    latest = _latest_occurrences(parsed)
    live = _fit_byte_budget(latest, candidates)
    return (
        live
        if max_images is None
        else _fit_image_cap(latest, set(live), max_images)
    )


def _latest_occurrences(
    parsed: Sequence[Sequence[Attachment]],
) -> list[Attachment]:
    """Каждая картинка один раз — в месте последнего вхождения, по порядку истории."""
    last: dict[str, tuple[int, Attachment]] = {}
    position = 0
    for attachments in parsed:
        for att in attachments:
            last[att.id] = (position, att)
            position += 1
    return [att for _, att in sorted(last.values(), key=lambda pair: pair[0])]


def _live_candidates(
    ordered: Sequence[Mapping[str, Any]],
    parsed: Sequence[Sequence[Attachment]],
    images: ImagesConfig,
) -> set[str]:
    """Живые по окнам и штукам (до бюджета и потолка): ваши картинки — самые свежие
    за последние ``LIVE_USER_TURNS`` ходов, снимки агента — самые свежие за текущий
    ход или те, после которых модель ещё не отвечала. Идём с конца истории."""
    turns = _turn_numbers(ordered)
    current = turns[-1] if turns else 0
    oldest_live_turn = current - LIVE_USER_TURNS + 1
    user_ids: list[str] = []
    tool_ids: list[str] = []
    answered = False  # модель отвечала после этого места (идём с конца)
    for message, attachments, turn in zip(
        reversed(ordered), reversed(parsed), reversed(turns)
    ):
        for att in reversed(attachments):
            if att.origin == ORIGIN_USER and turn >= oldest_live_turn:
                _take(user_ids, att.id, images.live_user_images)
            elif att.origin == ORIGIN_TOOL and (
                turn == current or not answered
            ):
                _take(tool_ids, att.id, images.live_tool_images)
        answered = answered or _is_model_output(message)
    return {*user_ids, *tool_ids}


def count_live_images(
    messages: Sequence[Mapping[str, Any]], live_ids: frozenset[str]
) -> int:
    """Сколько картинок уйдёт на провод: живые id в сообщениях запроса — каждая
    один раз, в последнем вхождении (``project_outbound``)."""
    return len(
        {
            att.id
            for message in _unique_by_id(messages)
            for att in attachments_of(message)
            if att.id in live_ids
        }
    )


def _unique_by_id(
    messages: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Сообщения по первому вхождению ``id`` — так же, как запрос склеивает state и
    батч (``create_call_data_completions``). Без ``id`` — каждое как есть."""
    seen: set[str] = set()
    out: list[Mapping[str, Any]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            if message_id in seen:
                continue
            seen.add(message_id)
        out.append(message)
    return out


def _turn_numbers(messages: Sequence[Mapping[str, Any]]) -> list[int]:
    """Номер хода каждого сообщения: число реальных реплик человека до него включительно."""
    numbers: list[int] = []
    turn = 0
    for message in messages:
        if extract_real_user_message_text(message):
            turn += 1
        numbers.append(turn)
    return numbers


def _is_model_output(message: Mapping[str, Any]) -> bool:
    """Ответ модели: текст ассистента или вызов тула."""
    return (
        message.get("role") == "assistant"
        or message.get("type") == "function_call"
    )


def _take(chosen: list[str], att_id: str, limit: int) -> None:
    """Взять id в живые, если он новый и место под потолком ещё есть."""
    if att_id not in chosen and len(chosen) < limit:
        chosen.append(att_id)


def _base64_size(size: int) -> int:
    """Размер base64 от размера файла — столько картинка весит на проводе."""
    return (size + 2) // 3 * 4


def _fit_byte_budget(
    latest: list[Attachment], live: set[str]
) -> frozenset[str]:
    """Снять живость с самых старых, пока base64 живых картинок не влезет в бюджет."""
    sized = [
        (att.id, _base64_size(att.size)) for att in latest if att.id in live
    ]
    total = sum(size for _, size in sized)
    for att_id, size in sized:
        if total <= MAX_REQUEST_IMAGE_BYTES:
            break
        live.discard(att_id)
        total -= size
    return frozenset(live)


def _fit_image_cap(
    latest: list[Attachment], live: set[str], cap: int
) -> frozenset[str]:
    """Снять живость с самых старых, пока живых картинок не станет не больше
    потолка модели."""
    total = sum(1 for att in latest if att.id in live)
    for att in latest:
        if total <= cap:
            break
        if att.id in live:
            live.discard(att.id)
            total -= 1
    return frozenset(live)


def project_outbound(
    messages: list[dict[str, Any]],
    *,
    audience: Audience,
    live_ids: frozenset[str],
    api_mode: str,
    read_bytes: Callable[[Attachment], bytes | None],
) -> list[dict[str, Any]]:
    """Сообщения для провода (спека §5). Вход не мутируется; сообщение без ключа
    ``attachments`` уходит тем же объектом.

    Пикселями уходят только вложения из ``live_ids`` и только когда модель
    запроса видит (``audience.native``) в ``chat_completions``, каждая картинка
    один раз — в последнем вхождении, чьи байты читаются (``_pixel_places``); в
    остальных её вхождениях — записка «та же картинка приложена в другом месте»,
    она бывает и после пикселей. Прочие — записками на своём месте (``_note``).
    Ключ ``attachments`` на провод не уходит ни в одном режиме.

    ``chat_completions``: есть картинка — ``content`` списком частей в порядке
    вложений, последней частью — текст сообщения; картинок нет — строка «записки
    через перевод строки, затем текст». Ключ ``type`` снимается: форма
    ``type: "message"`` на проводе теряет нетекстовые части
    (``utils/format_messages._canonical_to_chat_completions_message``).
    ``responses``: всегда строка, прочие ключи сообщения — как были.
    """
    shown = (
        live_ids
        if audience.native is not None and api_mode == _CHAT_COMPLETIONS
        else frozenset()
    )
    places = _pixel_places(messages, shown, read_bytes)
    return [
        _project_message(
            message,
            message_key=_message_key(message, i),
            places=places,
            audience=audience,
            shown=shown,
            api_mode=api_mode,
        )
        if isinstance(message, dict) and "attachments" in message
        else message
        for i, message in enumerate(messages)
    ]


def _message_key(message: Mapping[str, Any], index: int) -> Any:
    """Сообщение запроса: по ``id`` (повтор — то же сообщение), без него — по месту."""
    return message.get("id") or index


#: Место пикселей картинки: (ключ сообщения, номер вложения, байты).
_Place = tuple[Any, int, bytes]


def _pixel_places(
    messages: list[dict[str, Any]],
    shown: frozenset[str],
    read_bytes: Callable[[Attachment], bytes | None],
) -> dict[str, _Place]:
    """Где живая картинка уходит пикселями: её последнее вхождение, чьи байты
    читаются (мёртвый путь в последнем месте — берём раньшее). Картинки без
    читаемого вхождения в словаре нет."""
    places: dict[str, _Place] = {}
    for att_id, found in _shown_occurrences(messages, shown).items():
        for key, j, att in reversed(found):
            data = read_bytes(att)
            if data is not None:
                places[att_id] = (key, j, data)
                break
    return places


def _shown_occurrences(
    messages: list[dict[str, Any]], shown: frozenset[str]
) -> dict[str, list[tuple[Any, int, Attachment]]]:
    """Вхождения живых картинок по порядку: (ключ сообщения, номер вложения,
    вложение). Повтор сообщения по ``id`` пропускается — запрос оставит первое."""
    seen: set[Any] = set()
    occurrences: dict[str, list[tuple[Any, int, Attachment]]] = {}
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        key = _message_key(message, i)
        if key in seen:
            continue
        seen.add(key)
        for j, att in enumerate(attachments_of(message)):
            if att.id in shown:
                occurrences.setdefault(att.id, []).append((key, j, att))
    return occurrences


def request_carries_images(
    audience: Audience, live_ids: frozenset[str]
) -> bool:
    """Запрос несёт картинки пикселями: модель запроса видит и живые есть."""
    return audience.native is not None and bool(live_ids)


def _project_message(
    message: dict[str, Any],
    *,
    message_key: Any,
    places: dict[str, _Place],
    audience: Audience,
    shown: frozenset[str],
    api_mode: str,
) -> dict[str, Any]:
    """Одно сообщение с вложениями: часть на вложение, затем текст сообщения.
    ``message_key`` — ключ сообщения в ``places``."""
    parts: list[dict[str, Any]] = []
    for j, att in enumerate(attachments_of(message)):
        place = places.get(att.id)
        if place is not None and place[:2] == (message_key, j):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": data_url(att.mime, place[2])},
                }
            )
            continue
        text = (
            STUB_SHOWN_ELSEWHERE.format(label=att.label, wh=wh_text(att))
            if place is not None
            else _note(att, audience, file_missing=att.id in shown)
        )
        parts.append({"type": "text", "text": text})
    text = user_content_to_text(message.get("content"))
    projected = {
        key: value for key, value in message.items() if key != "attachments"
    }
    if api_mode == _CHAT_COMPLETIONS:
        projected.pop("type", None)
    if any(part["type"] == "image_url" for part in parts):
        projected["content"] = [*parts, {"type": "text", "text": text}]
    else:
        projected["content"] = "\n".join(
            [*(part["text"] for part in parts), text]
        )
    return projected


def _note(att: Attachment, audience: Audience, *, file_missing: bool) -> str:
    """Записка на месте картинки (спека §5). Подсказка про ``fs_view_image`` вшита
    в тексты «вне контекста» и «модель не видит», поэтому без тула в наборе или
    без смотрящего (``not audience.tool_visible``) уходит «видеть некому».
    Модель запроса известна слепой (``audience.blind_alias``) — записка с её
    именем: позвать тул или попросить пользователя."""
    if file_missing:
        template = STUB_FILE_MISSING
    elif audience.blind_alias is not None:
        template = (
            STUB_MODEL_REJECTS_VIEW
            if audience.tool_visible
            else STUB_MODEL_REJECTS_NOBODY
        )
        return template.format(
            label=att.label,
            wh=wh_text(att),
            alias=audience.blind_alias,
            path=att.path,
        )
    elif audience.probe_reason and not audience.tool_visible:
        return STUB_PROBE_FAILED.format(
            label=att.label, wh=wh_text(att), reason=audience.probe_reason
        )
    elif not audience.tool_visible:
        template = STUB_NOBODY
    elif audience.native is None:
        template = (
            STUB_MODEL_BLIND if audience.eyes is not None else STUB_UNCHECKED
        )
    else:
        template = STUB_OUT_OF_CONTEXT
    return template.format(label=att.label, wh=wh_text(att), path=att.path)
