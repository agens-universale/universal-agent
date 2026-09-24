from gc_cli import onboarding_wizard as wz
from gc_core import onboarding


def _io(answers):
    it = iter(answers)
    out = []
    return wz.WizardIO(
        reader=lambda p, secret: next(it), printer=out.append
    ), out


def test_auto_asks_only_pending_and_wraps_token():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "model": "gigacode_agent_v1.6_rc",  # задан → не спрашивается
    }
    # endpoint, затем auth-меню: "1" (ввести токен), затем сам токен
    io, _ = _io(["https://x/v1/chat/completions", "1", "sk-1"])
    values = wz.collect_fields(io, cfg, review_all=False)
    assert values == {
        "completions_url": "https://x/v1/chat/completions",
        "Authorization": "Bearer sk-1",
    }


def test_skip_leaves_field_out():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {
            "Authorization": onboarding.TOKEN_SENTINEL
        },
        "model": "m",
    }
    io, _ = _io(["", ""])  # оба пропущены
    assert wz.collect_fields(io, cfg, review_all=False) == {}


def test_review_all_asks_all_three(monkeypatch):
    cfg = {
        "completions_url": "https://old/v1",
        "chat_completion_headers": {"Authorization": "Bearer old"},
        "model": "old-model",
    }
    # Список моделей не участвует в проверке — без подмены шаг «Модель» реально
    # стучится на «https://new/v1» (зелёный тест сейчас — только потому, что DNS
    # не резолвит этот хост).
    monkeypatch.setattr(
        "gc_core.model_picker.fetch_models", lambda *a, **k: None
    )
    # Порядок: endpoint → auth-меню ("1" + токен) → model → уровни (Enter — пропустить)
    io, _ = _io(["https://new/v1", "1", "sk-new", "new-model", ""])
    values = wz.collect_fields(io, cfg, review_all=True)
    assert values == {
        "completions_url": "https://new/v1",
        "Authorization": "Bearer sk-new",
        "model": "new-model",
    }


def test_invalid_endpoint_reask_then_enter_skips():
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer set"},
        "model": "m",
    }
    io, _ = _io(["ftp://weird", ""])  # невалид → переспрос → Enter = пропуск
    assert wz.collect_fields(io, cfg, review_all=False) == {}
