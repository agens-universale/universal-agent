"""``fs_view_image`` — посмотреть изображение на диске (спека §9.2).

Картинку видит модель запроса, на ответ которого пришёл вызов, — тогда она едет
техническим сообщением после батча (``state.pending_tool_attachments``); иначе
честный отказ (кандидат «глаза» — ``vision/eyes``). Кто смотрит, решает
контроллер на каждый запрос (``state.vision_audience``) — тул в контроллер не ходит.

Чтение: картинки класса ``media`` (хранилище картинок тредов, MCP-медиа) лежат у
агента — их тул читает с хоста; остальные пути — через ``os_backend`` (в Harbor —
из контейнера) под обычными правилами чтения. Нормализация (Pillow) и запись
копии — CPU и диск на сотни миллисекунд, поэтому они в ``asyncio.to_thread``;
само чтение байт инлайн, как у остальных файловых тулов.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from gc_core.attachments.model import (
    MAX_INPUT_BYTES,
    ORIGIN_TOOL,
    TOOL_LABEL,
    Attachment,
    attachments_of,
)
from gc_core.attachments.normalize import (
    REASON_NOT_AN_IMAGE,
    REASON_TOO_LARGE,
    NormalizedImage,
    NormalizeError,
    normalize_image,
)
from gc_core.attachments.store import image_id
from gc_core.attachments.texts import (
    TOOL_ALREADY_IN_CONTEXT,
    TOOL_ATTACHED,
    TOOL_NO_IMAGE_STORE,
    TOOL_NO_VISION,
    TOOL_TOO_MANY_IN_STEP,
    TOOL_VISION_FAILED,
    VIEW_DEFAULT_QUESTION,
)
from gc_core.config_modules.images import read_images_config
from gc_core.constants.tool_names import FS_VIEW_IMAGE_NAME
from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.security.media_roots import media_roots
from gc_core.security.path_policy import resolve_tool_context
from gc_core.security.protected_paths import classify_path, protected_paths
from gc_core.security.read_guard import enforce_read_guard
from gc_core.thread_paths import thread_address
from gc_core.tools.schema_optional import coalesce_optional_string
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.backend_errors import (
    http_status_code,
    is_image_rejection_error,
)
from gc_core.utils.tool_report_paths import tool_report_abspath
from gc_core.vision.errors import (
    ERR_NO_IMAGE_STORE,
    ERR_NO_VISION_MODEL,
    ERR_TOO_MANY_IMAGES,
    ERR_VISION_FAILED,
    REASON_EMPTY_RESPONSE,
    REASON_HTTP,
)
from gc_core.vision.eyes import adescribe_image, task_context_of
from gc_core.vision.notes import note_blind
from gc_core.vision.rejects import mark_blind

_TOOL_DESCRIPTION = (
    "View an image file (PNG, JPEG, GIF, WEBP, BMP, TIFF). "
    "Call it when a note in the history points to an image file, to inspect an image "
    "in the project, or to look at a screenshot an MCP tool returned: pass the path "
    'from the image block of that tool result ("type": "image", "path": ...), '
    "not a path mentioned in its text. "
    "An image attached to a message (not a note) is already in front of you — "
    "answer from it, do not call this tool for it. "
    "If you can see images, the image arrives in the next message; otherwise you get "
    "a text description of it. Use question to say what to look for."
)

# Ответы на ошибки входа — для модели (EN, как у остальных файловых тулов).
MSG_NOT_FOUND = "File not found: {path}"
MSG_NOT_AN_IMAGE = "Not an image, or the format is not supported (PNG, JPEG, GIF, WEBP, BMP, TIFF)."
MSG_TOO_LARGE = "The image is too large to view."
MSG_READ_DENIED = "Read denied by the security policy: {reason}"

_NORMALIZE_MESSAGES = {
    REASON_NOT_AN_IMAGE: MSG_NOT_AN_IMAGE,
    REASON_TOO_LARGE: MSG_TOO_LARGE,
}


def _failure_reason(exc: BaseException) -> str:
    """Короткая причина сбоя «глаз»: код HTTP или имя исключения."""
    status = http_status_code(exc)
    if status is None:
        status = getattr(exc, "status_code", None)
    return REASON_HTTP.format(status=status) if status else type(exc).__name__


def _normalized(data: bytes) -> NormalizedImage:
    """Нормализация (CPU, Pillow) — вне loop: зовётся через ``asyncio.to_thread``."""
    try:
        return normalize_image(data)
    except NormalizeError as exc:
        raise _InputError(exc.reason, _NORMALIZE_MESSAGES[exc.reason]) from exc


def _step_snapshot_ids(state: Any) -> set[str]:
    """Снимки тула, уже прикреплённые в этом шаге (ждут хвоста батча)."""
    return {
        att.get("id")
        for att in getattr(state, "pending_tool_attachments", ())
        if att.get("origin") == ORIGIN_TOOL
    }


class _InputError(Exception):
    """Файл нельзя показать: тип ошибки входа и текст для модели."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type


@loop_warn_on_repeat(**READ_PROFILE)
class ViewImage(AbstractSimpleTool):
    @classmethod
    def tool_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": FS_VIEW_IMAGE_NAME,
            "description": _TOOL_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path of the image file.",
                    },
                    "question": {
                        "type": "string",
                        "description": "What to look for in the image.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(
        cls, config_dict: Optional[Dict[str, Any]] = None
    ) -> str:
        return f"Tool name: {FS_VIEW_IMAGE_NAME}\nTool description: {_TOOL_DESCRIPTION}\n"

    @enforce_read_guard(path_arg="path")
    async def acall(
        self, path: str, question: Optional[str] = None, **kwargs
    ) -> str:
        audience = getattr(self._state, "vision_audience", None)
        if audience is None or audience.nobody:
            # Скрытый тул диспетчер всё равно исполняет (ищет его в полном
            # ctx.tools_data): честный отказ, а не unknown_tool (спека §9.2).
            return self._no_vision(path)
        if getattr(self._ctx, "image_store", None) is None:
            # Контекст, собранный в обход фабрики: снимок положить некуда.
            return self._error(path, ERR_NO_IMAGE_STORE, TOOL_NO_IMAGE_STORE)
        try:
            data = await self._aread_source(path)
            image = await asyncio.to_thread(_normalized, data)
        except _InputError as exc:
            return self._error(path, exc.error_type, str(exc))
        if audience.native is not None:
            return await self._aattach(path, image)
        att = await asyncio.to_thread(
            self._store, image, self._report_path(path)
        )
        return await self._adescribe(path, att, question, audience.eyes)

    async def _aattach(self, path: str, image: NormalizedImage) -> str:
        """Модель запроса видит сама — снимок в хвост батча. Решение — по id до
        записи: копия в тред ложится только у прикреплённого снимка."""
        att_id = image_id(image.data)
        if att_id in getattr(self._state, "vision_live_ids", frozenset()):
            # Уже ушла целиком в этом запросе: второй снимок нёс бы вторую копию
            # base64 на каждом шаге, пока она живая, а нового не показал бы —
            # `question` модели, которая видит сама, не нужен.
            return self._already_in_context(path, att_id)
        step = _step_snapshot_ids(self._state)
        if att_id not in step:  # та же картинка второй раз за шаг — одно место
            refusal = self._step_limit_refusal(path, len(step))
            if refusal is not None:
                return refusal
            att = await asyncio.to_thread(
                self._store, image, self._report_path(path)
            )
            self._state.pending_tool_attachments.append(att.to_dict())
        return self._attached(path, image)

    def _step_limit_refusal(self, path: str, taken: int) -> Optional[str]:
        """Лишний снимок шага модель не увидела бы: пикселями уходят только
        `live_tool_images` последних, а «attached» звало бы смотреть снова."""
        limit = read_images_config(
            getattr(self._ctx, "config_dict", None)
        ).live_tool_images
        if taken < limit:
            return None
        return self._error(
            path,
            ERR_TOO_MANY_IMAGES,
            TOOL_TOO_MANY_IN_STEP.format(n=limit),
            limit=limit,
        )

    async def _aread_source(self, path: str) -> bytes:
        if self._is_media(path):
            return self._read_host(path)
        return await self._aread_backend(path)

    def _is_media(self, path: str) -> bool:
        """Картинки хранилища и MCP-медиа — у агента, не в среде исполнения."""
        cfg = getattr(self._ctx, "config_dict", None) or {}
        roots = media_roots(cfg)
        launch_root = resolve_tool_context(self).launch_root
        return (
            classify_path(path, protected_paths(cwd=launch_root, media=roots))
            == "media"
        )

    def _read_host(self, path: str) -> bytes:
        if not os.path.isfile(path):
            raise _InputError("not_found", MSG_NOT_FOUND.format(path=path))
        if os.path.getsize(path) > MAX_INPUT_BYTES:
            raise _InputError(REASON_TOO_LARGE, MSG_TOO_LARGE)
        try:
            return Path(path).read_bytes()
        except OSError as exc:
            raise _InputError(
                "read_denied", MSG_READ_DENIED.format(reason=exc)
            ) from exc

    async def _aread_backend(self, path: str) -> bytes:
        backend = self._backend
        if not await backend.is_file(path):
            raise _InputError("not_found", MSG_NOT_FOUND.format(path=path))
        if await backend.getsize(path) > MAX_INPUT_BYTES:
            raise _InputError(REASON_TOO_LARGE, MSG_TOO_LARGE)
        try:
            return await backend.read_bytes(path)
        except OSError as exc:
            raise _InputError(
                "read_denied", MSG_READ_DENIED.format(reason=exc)
            ) from exc

    def _store(self, image: NormalizedImage, source: str) -> Attachment:
        """Копия снимка в тред — вне loop: зовётся через ``asyncio.to_thread``."""
        store = self._ctx.image_store
        address = thread_address(self._ctx)
        if address is None:
            # Прогон без треда (CLI без -t): копия остаётся в кармане процесса.
            return store.stage(
                image, origin=ORIGIN_TOOL, label=TOOL_LABEL, source=source
            )
        system_id, thread_id = address
        return store.snapshot(
            image,
            system_id,
            thread_id,
            origin=ORIGIN_TOOL,
            label=TOOL_LABEL,
            source=source,
        )

    def _report_path(self, path: str) -> str:
        return tool_report_abspath(path, workspace=self._workspace)

    def _attached(self, path: str, image: NormalizedImage) -> str:
        return json.dumps(
            {
                "ok": True,
                "attached": True,
                "path": self._report_path(path),
                "width": image.width,
                "height": image.height,
                "message": TOOL_ATTACHED,
            },
            ensure_ascii=False,
        )

    def _already_in_context(self, path: str, att_id: str) -> str:
        return json.dumps(
            {
                "ok": True,
                "already_in_context": True,
                "path": self._report_path(path),
                "message": TOOL_ALREADY_IN_CONTEXT.format(
                    label=self._live_label(att_id)
                ),
            },
            ensure_ascii=False,
        )

    def _live_label(self, image_id: str) -> str:
        """Подпись, под которой модель видит эту картинку в истории (плашка человека
        или снимок тула); не нашлась — «this image»."""
        for message in getattr(self._state, "messages", None) or []:
            for att in attachments_of(message):
                if att.id == image_id:
                    return att.label
        return "this image"

    def _error(
        self, path: str, error_type: str, message: str, **extra: Any
    ) -> str:
        return json.dumps(
            {
                "ok": False,
                "error": {"type": error_type, "message": message, **extra},
                "path": self._report_path(path),
            },
            ensure_ascii=False,
        )

    def _no_vision(self, path: str) -> str:
        return self._error(path, ERR_NO_VISION_MODEL, TOOL_NO_VISION)

    async def _adescribe(
        self, path: str, att: Attachment, question: Optional[str], eyes: Any
    ) -> str:
        """Кандидат 2 — «глаза»: модель запроса не видит, описывает модель для зрения."""
        try:
            description = await adescribe_image(
                self._ctx,
                self._state,
                att,
                question=coalesce_optional_string(question)
                or VIEW_DEFAULT_QUESTION,
                task_context=task_context_of(self._state),
            )
        except Exception as exc:
            if is_image_rejection_error(exc):
                # Модель запроса картинку не видит, а «глаза» теперь помечены —
                # смотреть некому, пересчитывать «кто смотрит» не нужно (§10).
                cfg = getattr(self._ctx, "config_dict", None) or {}
                if mark_blind(
                    eyes, fallback_url=str(cfg.get("completions_url") or "")
                ):
                    note_blind(
                        self._ctx,
                        eyes,
                        eyes=True,
                        logger=getattr(self._ctx, "logger", None),
                    )
                return self._no_vision(path)
            return self._vision_failed(path, _failure_reason(exc))
        if not description:
            return self._vision_failed(path, REASON_EMPTY_RESPONSE)
        return json.dumps(
            {
                "ok": True,
                "description": description,
                "path": self._report_path(path),
            },
            ensure_ascii=False,
        )

    def _vision_failed(self, path: str, reason: str) -> str:
        return self._error(
            path,
            ERR_VISION_FAILED,
            TOOL_VISION_FAILED.format(reason=reason),
            reason=reason,
        )

    def _read_guard_blocked_result(
        self,
        error: Dict[str, Any],
        path: str = "",
        question: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """Отказ ``read_guard`` (пол ``.gc``, периметр) — в словаре ошибок тула."""
        reason = (
            error.get("message")
            or error.get("reason")
            or error.get("type")
            or ""
        )
        return self._error(
            path or str(error.get("path") or ""),
            "read_denied",
            MSG_READ_DENIED.format(reason=reason),
        )
