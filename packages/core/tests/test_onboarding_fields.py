def test_auth_helpers_live_in_core():
    from gc_core import onboarding

    assert onboarding.authorization_header("sk") == "Bearer sk"
    assert onboarding.authorization_header("Basic abc") == "Basic abc"
    assert onboarding.mask_authorization("Bearer secret").startswith(
        "Bearer sec"
    )


def test_authorization_attempts_try_bare_key_and_bearer():
    """Ключ без схемы пробуется как вставлен и с префиксом Bearer.
    Уже собранный заголовок Bearer даёт и голый ключ — сервер мог ждать
    ровно одну из двух форм. Basic не переписываем."""
    from gc_core import onboarding

    assert onboarding.authorization_attempts("sk-abc") == [
        "sk-abc",
        "Bearer sk-abc",
    ]
    assert onboarding.authorization_attempts("  sk-abc  ") == [
        "sk-abc",
        "Bearer sk-abc",
    ]
    assert onboarding.authorization_attempts("Bearer sk-abc") == [
        "Bearer sk-abc",
        "sk-abc",
    ]
    assert onboarding.authorization_attempts("bearer sk-abc") == [
        "bearer sk-abc",
        "sk-abc",
    ]
    assert onboarding.authorization_attempts("Basic abc") == ["Basic abc"]
    assert onboarding.authorization_attempts("") == []
    assert onboarding.authorization_attempts(None) == []


def test_mask_secret_shows_recognisable_edges():
    """Голый ключ (`gsk_…`, `sk-…`) — не заголовок: схемы у него нет, и «схема + первые
    три» вырождались в «***». Человек узнаёт свой ключ по краям — их и показываем."""
    from gc_core import onboarding

    assert onboarding.mask_secret("gsk_8xEXAMPLEabcdefghVZdf4") == "gsk_…Zdf4"
    assert (
        onboarding.mask_secret("0123456789abcdef") == "0123…cdef"
    )  # ровно граница (16)
    assert onboarding.mask_secret("sk-abc") == "***"  # короткий не режем
    assert onboarding.mask_secret("") == "***"


def test_mask_secret_never_shows_more_than_it_hides():
    """Края занимают 8 символов, поэтому порог — 16, а не 10: ключ из 10 знаков
    маскировался в «8 показано, 2 скрыто» — ровно тот случай, ради которого правило
    «короткие не режем» и заводили."""
    from gc_core import onboarding

    assert onboarding.mask_secret("0123456789") == "***"  # 10: скрыто < видно
    assert (
        onboarding.mask_secret("0123456789abcde") == "***"
    )  # 15: всё ещё нет
    masked = onboarding.mask_secret("0123456789abcdef")  # 16: краёв ≤ середины
    assert masked.count("…") == 1 and len(masked.replace("…", "")) == 8


def test_display_secret_routes_header_and_bare_key():
    """display_secret выбирает маску по форме значения: заголовок «схема секрет» —
    прежней mask_authorization (схему показать осмысленно), голый ключ — mask_secret."""
    from gc_core import onboarding

    assert onboarding.display_secret("Bearer sk-abcdef") == "Bearer sk-***"
    assert (
        onboarding.display_secret("gsk_8xEXAMPLEabcdefghVZdf4") == "gsk_…Zdf4"
    )
    # ссылка секретом не является — показывается как есть (+ статус переменной)
    assert onboarding.display_secret("${ENV:VOICE_TOKEN}").startswith(
        "${ENV:VOICE_TOKEN}"
    )


def test_onboarding_fields_shape():
    from gc_core import onboarding_fields as f

    assert [fs.key for fs in f.ONBOARDING_FIELDS] == [
        "completions_url",
        "Authorization",
        "model",
    ]
    assert f.ONBOARDING_FIELDS[0].label == "Адрес сервера"
    assert "http" in f.ONBOARDING_FIELDS[0].hint
    assert len(f.AUTH_OPTIONS) == 3
    assert "инструменты (tools)" in f.MODEL_WARN
