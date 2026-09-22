"""C1 Часть 1: робастность судьи модерации к мусорному выводу слабой модели.

Нормализация ФОРМАТА вердикта (пробел/дефис/регистр) + один ретрай судьи.
Security НЕ ослабляется: синонимы в 'safe' не маппим, неузнанный вердикт по-прежнему
отвергается (→ декоратор → fail-closed)."""

import pytest
from gc_core.security import command_moderation as cm
from gc_core.security.command_moderation import validate_judge_result


def test_validate_normalizes_verdict_separators_and_case():
    # «potentially dangerous» через пробел / дефис / в другом регистре — частый
    # формат слабой модели; нормализуем, не падаем.
    assert (
        validate_judge_result({"verdict": "potentially dangerous", "risk_flags": ["x"]}).verdict == cm.DANGEROUS_VERDICT
    )
    assert (
        validate_judge_result({"verdict": "potentially-dangerous", "risk_flags": ["x"]}).verdict == cm.DANGEROUS_VERDICT
    )
    assert validate_judge_result({"verdict": "  SAFE  "}).verdict == cm.SAFE_VERDICT


def test_validate_still_rejects_unknown_verdict():
    # security: неузнанный вердикт отвергается (НЕ маппим в safe).
    with pytest.raises(ValueError):
        validate_judge_result({"verdict": "maybe"})
    # 'unsafe' содержит 'safe', но не должен стать safe (без подстрочного матча).
    with pytest.raises(ValueError):
        validate_judge_result({"verdict": "unsafe"})


def test_retry_recovers_on_second_attempt():
    # разовый мусорный вердикт не роняет судью — второй вызов отдаёт валидный.
    calls = {"n": 0}

    def client(payload):
        calls["n"] += 1
        return {"verdict": "nonsense"} if calls["n"] == 1 else {"verdict": "safe"}

    assert cm.run_command_judge_with_retry(client, {}, retries=1).verdict == cm.SAFE_VERDICT
    assert calls["n"] == 2


def test_retry_raises_after_exhausting():
    # стабильный мусор → пробрасываем (декоратор переведёт в judge_unavailable → fail-closed).
    with pytest.raises(ValueError):
        cm.run_command_judge_with_retry(lambda p: {"verdict": "nonsense"}, {}, retries=1)
