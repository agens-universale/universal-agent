from pydantic import BaseModel, ConfigDict


class UpdateConfig(BaseModel):
    """Параметры обновления агента (спека D). Значения читаются рантаймом через
    updater.check-геттеры из сырого dict; схема нужна, чтобы extra='forbid'
    верхнего AppConfig не отвергал ключ update в config.yml."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # YAML разворачивает голое `off` в bool False → принимаем str|bool; смысл «выкл»
    # даёт и "off", и False (см. updater.check.auto_check_enabled).
    check: str | bool = (
        "auto"  # auto | off — фоновая проверка при каждом старте TUI
    )
    extra_args: list[
        str
    ] = []  # доп. аргументы к uv tool install (корп: ["--system-certs"])
