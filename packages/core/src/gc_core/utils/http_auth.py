"""Технические варианты значения HTTP-заголовка Authorization."""


def authorization_attempts(raw: str | None) -> list[str]:
    """Сначала заданная форма, затем raw/Bearer альтернатива при её наличии."""
    if raw is None:
        return []
    value = raw.strip()
    if not value:
        return []
    attempts = [value]
    if value.lower().startswith("bearer "):
        token = value.split(" ", 1)[1].strip()
        if token and " " not in token:
            attempts.append(token)
    elif " " not in value:
        attempts.append(f"Bearer {value}")
    return attempts
