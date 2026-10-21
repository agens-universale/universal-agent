"""Технические варианты значения HTTP-заголовка Authorization."""

# Форма Authorization ("bearer" / "raw"), с которой запрос прошёл, — на
# процесс. Ключ — URL, модель и форма, в которой значение задано: «так
# заданное значение для этой модели на этом URL слать так» (шлюз может
# разводить модели одного URL по бэкендам с разной авторизацией). Токен в
# ключ не входит; записей не больше двух на пару URL и модели из конфига.
_ACCEPTED_FORMS: dict[tuple[str, str | None, str], str] = {}


def authorization_attempts(raw: str | None) -> list[str]:
    """Сначала заданная форма, затем raw/Bearer альтернатива при её наличии."""
    if raw is None:
        return []
    value = raw.strip()
    if not value:
        return []
    attempts = [value]
    if _form(value) == "bearer":
        token = value.split(" ", 1)[1].strip()
        if token and " " not in token:
            attempts.append(token)
    elif " " not in value:
        attempts.append(f"Bearer {value}")
    return attempts


def server_authorization_attempts(
    url: str, model: str | None, raw: str | None
) -> list[str]:
    """Формы `authorization_attempts`; принятая для модели на URL — первой."""
    attempts = authorization_attempts(raw)
    accepted = _ACCEPTED_FORMS.get(_memory_key(url, model, attempts))
    return sorted(attempts, key=lambda value: _form(value) != accepted)


def remember_accepted_authorization(
    url: str, model: str | None, raw: str | None, accepted: str | None
) -> None:
    """Запомнить форму, с которой запрос прошёл успешно."""
    key = _memory_key(url, model, authorization_attempts(raw))
    if key is not None and accepted is not None:
        _ACCEPTED_FORMS[key] = _form(accepted)


def _memory_key(
    url: str, model: str | None, attempts: list[str]
) -> tuple[str, str | None, str] | None:
    """URL, модель и форма заданного значения; None — выбирать не из чего."""
    if len(attempts) < 2:
        return None
    return url, model, _form(attempts[0])


def _form(value: str) -> str:
    return "bearer" if value.lower().startswith("bearer ") else "raw"
