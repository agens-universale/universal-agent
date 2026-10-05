"""Ошибки зрения в ответе тула ``fs_view_image`` — общий словарь тула и фронтов.

``error.type`` — тип ошибки, ``error.reason`` — короткая причина сбоя «глаз»: код
HTTP, имя исключения или пустой ответ. По ним TUI рисует свою сводку строки вызова,
не разбирая английский текст для модели. Модуль лёгкий — без импортов ядра:
фронт берёт константы, не загружая сам тул.
"""

ERR_NO_VISION_MODEL = "no_vision_model"
ERR_VISION_FAILED = "vision_failed"
ERR_TOO_MANY_IMAGES = "too_many_images"
ERR_NO_IMAGE_STORE = "no_image_store"

#: Причина ``error.reason`` у ``ERR_VISION_FAILED``; иначе — имя исключения.
REASON_HTTP = "HTTP {status}"
REASON_EMPTY_RESPONSE = "empty response"
