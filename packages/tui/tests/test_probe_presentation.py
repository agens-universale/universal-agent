"""probe_line_text: рендер строки статуса пробы — чистые юниты без пилота."""
import pytest

pytest.importorskip("textual")

from gc_core.model_picker import ModelInfo
from gc_tui.services.probe_presentation import probe_line_text
from gc_tui.services.settings_probe import ProbeResult


def _ok(models, detail="доступен", latency_ms=42):
    return ProbeResult(verdict="ok", http_status=200, models=models,
                       latency_ms=latency_ms, detail=detail)


def test_none_result_is_not_checked():
    assert probe_line_text(None).plain == "— не проверено"


def test_ok_without_models_no_tail():
    r = _ok(None)
    assert probe_line_text(r).plain == "✓ доступен · 42 ms"


def test_ok_with_models_count_no_model_id():
    r = _ok([ModelInfo(id="a"), ModelInfo(id="b")])
    assert probe_line_text(r).plain == "✓ доступен · 42 ms · 2 модели на сервере"


def test_ok_model_id_found_appends_tail():
    r = _ok([ModelInfo(id="glm-5"), ModelInfo(id="other")])
    text = probe_line_text(r, model_id="glm-5")
    assert text.plain == "✓ доступен · 42 ms · 2 модели на сервере · glm-5 найдена на сервере"


def test_ok_model_id_not_found_replaces_whole_line_with_warning():
    r = _ok([ModelInfo(id="other")])
    text = probe_line_text(r, model_id="glm-5")
    assert text.plain == "⚠ модель glm-5 на сервере не найдена"


def test_ok_model_id_given_but_models_none_has_no_tail():
    r = _ok(None)
    text = probe_line_text(r, model_id="glm-5")
    assert text.plain == "✓ доступен · 42 ms"


def test_rejected():
    r = ProbeResult(verdict="rejected", http_status=401, models=None,
                    latency_ms=5, detail="токен отклонён (401)")
    assert probe_line_text(r).plain == "✗ токен отклонён (401)"


def test_unknown():
    r = ProbeResult(verdict="unknown", http_status=None, models=None,
                    latency_ms=0, detail="нет соединения (timeout)")
    assert probe_line_text(r).plain == "⚠ не удалось проверить: нет соединения (timeout)"


# ── save_warning: реплика рядом с «сохранено», когда проба закончилась не «ок» ──
#
# Читает смысл из auth_state/offline, а не из verdict: «rejected» бывает и про имя
# модели, «unknown» — вердиктом канарейки при живом сервере (разбор ревью 2026-08-18).

def test_save_warning_is_silent_when_there_is_nothing_to_say():
    from gc_tui.services.probe_presentation import save_warning
    assert save_warning(None) == ""
    assert save_warning(_ok(None)) == ""


def test_save_warning_blames_the_token_when_the_token_was_judged():
    from gc_tui.services.probe_presentation import save_warning
    r = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)", auth_state="rejected")
    assert save_warning(r) == "сервер отклонил токен — проверьте"


def test_save_warning_names_the_missing_token_not_a_rejected_one():
    """Токена не было вовсе: сервер отклонил контрольный запрос без него. Отклонять
    было нечего — «сервер отклонил токен» здесь неправда."""
    from gc_tui.services.probe_presentation import save_warning
    r = ProbeResult("rejected", 401, None, 5, "сервер требует авторизацию (401)",
                    auth_state="required")
    assert "отклонил" not in save_warning(r)
    assert "требует авторизацию" in save_warning(r)


def test_save_warning_does_not_call_a_model_404_a_token_problem():
    """Живой GigaCode на неизвестную модель отвечает 404: вердикт «rejected», но он
    про имя модели. Прежний текст называл это отклонённым токеном."""
    from gc_tui.services.probe_presentation import save_warning
    r = ProbeResult("rejected", 404, None, 5,
                    "отклонено сервером (404): проверьте адрес и имя модели")
    text = save_warning(r)
    assert "токен" not in text
    assert "проверьте адрес и имя модели" in text


def test_save_warning_does_not_call_a_live_server_unreachable():
    """Канарейка: сервер ОТВЕТИЛ, просто так же, как на заведомо неверный токен —
    вердикт «unknown» при живом сервере. Прежний текст объявлял его недоступным."""
    from gc_tui.services.probe_presentation import save_warning
    r = ProbeResult("unknown", 400, None, 5,
                    "не удалось подтвердить токен: на заведомо неверный сервер "
                    "отвечает так же (400)")
    text = save_warning(r)
    assert "недоступен" not in text
    assert "заведомо неверный" in text


def test_save_warning_blames_the_server_only_when_it_did_not_answer():
    from gc_tui.services.probe_presentation import save_warning
    r = ProbeResult("unknown", None, None, 5, "нет соединения (timeout)", offline=True)
    assert save_warning(r) == "проверить не удалось — сервер недоступен"


# ── token_accepted: единственное место, где из результата достают вердикт О ТОКЕНЕ ──

def test_token_accepted_trusts_the_differential_verdict():
    from gc_tui.services.probe_presentation import token_accepted
    accepted = ProbeResult("ok", 200, None, 5, "токен принят", auth_state="accepted")
    not_required = ProbeResult("ok", 200, None, 5, "доступ без токена",
                               auth_state="not_required")
    rejected = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)",
                           auth_state="rejected")
    assert token_accepted(accepted, has_token=True) is True
    assert token_accepted(not_required, has_token=True) is True
    assert token_accepted(rejected, has_token=True) is False


def test_token_accepted_says_nothing_about_a_public_models_route():
    """Справочник моделей ответил 200 — но он бывает публичным (OpenRouter), и о
    токене это не говорит ничего. Прежний глиф хаба читал такой ответ как «токен
    в порядке» и расходился со строкой секции."""
    from gc_tui.services.probe_presentation import token_accepted
    from gc_core.model_picker import ModelInfo
    r = ProbeResult("ok", 200, [ModelInfo(id="m")], 10, "доступен")
    assert token_accepted(r, has_token=True) is None


def test_token_accepted_reads_the_real_route_without_the_differential():
    """Моделей в результате нет — значит отвечал сам chat/completions (проба уходит
    туда фолбэком). Не-401 там означает, что запрос прошёл авторизацию и спорит уже
    о теле: живой GigaCode на неверное имя модели отвечает 404 при рабочем токене."""
    from gc_tui.services.probe_presentation import token_accepted
    r = ProbeResult("rejected", 404, None, 12,
                    "отклонено сервером (404): проверьте адрес и имя модели")
    assert token_accepted(r, has_token=True) is True


def test_token_accepted_reads_a_401_without_the_differential_too():
    """401 — отказ на самом маршруте: достоверно и без differential."""
    from gc_tui.services.probe_presentation import token_accepted
    r = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")
    assert token_accepted(r, has_token=True) is False


def test_token_accepted_when_there_is_no_token_at_all():
    """Токена нет — значит запрос по реальному маршруту и был контрольным: ответил
    сервер осмысленно, авторизация не требуется; 401 — требуется, а предъявить
    нечего; иное — утверждать не о чем. Ответ справочника и тут не в счёт: он
    отвечает без токена всем, а chat/completions может его требовать."""
    from gc_tui.services.probe_presentation import token_accepted
    from gc_core.model_picker import ModelInfo
    ok = ProbeResult("ok", 200, None, 10, "доступен")
    needs = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")
    weird = ProbeResult("unknown", 500, None, 5, "сервер ответил 500")
    catalog = ProbeResult("ok", 200, [ModelInfo(id="m")], 10, "доступен")
    assert token_accepted(ok, has_token=False) is True
    assert token_accepted(needs, has_token=False) is False
    assert token_accepted(weird, has_token=False) is None
    assert token_accepted(catalog, has_token=False) is None
