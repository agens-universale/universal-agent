"""Медиум-независимое ядро онбординга: декларатив полей + общий копи.
Потребляют оба фронтенда (плоский onboarding_wizard, Textual OnboardingScreen)."""
from __future__ import annotations
from dataclasses import dataclass

ADDRESS_HINT = "напр. https://host/v1/chat/completions"
# Отказ структурной проверки адреса (`onboarding._valid_endpoint_url`) — один текст на
# все формы с полем адреса: копия в соседнем редакторе разошлась бы с оригиналом на
# первой же самостоятельной правке (тот же приём, что у статусов сетевого списка,
# `field_list.LIST_*`).
URL_ERROR = "нужны http(s):// и хост"
AUTH_OPTIONS = ("Bearer-токен", "без авторизации (токен не нужен)", "пропустить")
TOKEN_HINT = "токен (ввод скрыт)"
NO_AUTH_NOTE = "без авторизации — заголовок не отправляется"
NO_AUTH_LABEL = "без авторизации"
NO_AUTH_TOGGLE_HINT = "Space — переключить"
NO_AUTH_FROZEN_PLACEHOLDER = "не используется"
MODEL_NO_LIST = "список не получен — впишите имя вручную"
MODEL_WARN = ("отмеченные модели могут не поддерживать инструменты (tools) — "
              "с ними агент может работать некорректно")
MANUAL_ENTRY = "вписать вручную…"


@dataclass(frozen=True)
class FieldSpec:
    key: str      # ключ-якорь конфига
    label: str    # заголовок секции
    hint: str
    kind: str     # "endpoint" | "auth" | "model"


ONBOARDING_FIELDS = (
    FieldSpec("completions_url", "Адрес сервера", ADDRESS_HINT, "endpoint"),
    FieldSpec("Authorization", "Авторизация", "", "auth"),
    FieldSpec("model", "Модель", "", "model"),
)
