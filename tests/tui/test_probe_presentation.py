"""connection_line_text: рендер строки вердикта пробы — чистые юниты без пилота.

Строка одна на три места (секция «Подключение», форма записи модели, шаг мастера),
поэтому и таблица её слов живёт одна — здесь."""
import pytest

pytest.importorskip("textual")

from model_picker import ModelInfo
from tui.services.probe_presentation import connection_line_text
from tui.services.settings_probe import ProbeResult


def test_none_result_is_not_checked():
    assert connection_line_text(None).plain == "— не проверено"


def test_ok_with_an_accepted_token_says_both():
    r = ProbeResult("ok", 200, None, 42, "токен принят", auth_state="accepted")
    assert connection_line_text(r).plain == "✓ сервер доступен · ✓ токен принят"


def test_ok_without_authorization_names_that_instead_of_the_token():
    r = ProbeResult("ok", 200, None, 42, "доступ без токена — авторизация не требуется",
                    auth_state="not_required")
    assert connection_line_text(r).plain == "✓ сервер доступен · авторизация не требуется"


def test_ok_without_a_verdict_about_the_token_stays_neutral():
    """Сырой config-probe (auth_state=None) о токене не знает ничего: утверждать
    «принят» по одному «ок» сервера нечестно. Литерал в detail сам по себе тоже
    ничего не «включает» — вердикт только из auth_state."""
    assert connection_line_text(ProbeResult("ok", 200, None, 42, "доступен")).plain         == "✓ сервер доступен"
    text_only = ProbeResult("ok", 200, None, 42, "токен принят")   # без auth_state
    assert "токен принят" not in connection_line_text(text_only).plain


def test_401_with_a_token_blames_the_token_not_the_server():
    r = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)", auth_state="rejected")
    assert connection_line_text(r).plain == "✓ сервер доступен · ✗ токен отклонён (401)"


def test_401_without_a_token_says_the_server_wants_one():
    """Токена не было вовсе — отклонять было нечего: «токен отклонён» тут ложь."""
    r = ProbeResult("rejected", 401, None, 5, "сервер требует авторизацию (401)",
                    auth_state="required")
    assert connection_line_text(r).plain == "✓ сервер доступен · ⚠ сервер требует авторизацию (401)"


def test_only_a_network_failure_blames_the_server():
    r = ProbeResult("unknown", None, None, 0, "нет соединения (timeout)", offline=True)
    assert connection_line_text(r).plain == "✗ сервер недоступен"


def test_a_refusal_on_the_merits_is_a_refusal_not_a_failed_check():
    """Сервер ОТВЕТИЛ отказом (404 про путь/модель, нелатиница в токене) — проверка
    состоялась, и «не удалось проверить» было бы неправдой."""
    r = ProbeResult("rejected", 404, None, 12, "отклонено сервером (404): проверьте имя модели")
    assert connection_line_text(r).plain == "✗ отклонено сервером (404): проверьте имя модели"
    # Нелатиница в токене: запрос не ушёл вовсе (статуса нет), но данные назвал сам
    # редактор — валить это на лежачий сервер значит скрыть исправимую причину.
    bad_token = ProbeResult("rejected", None, None, 0,
                            "в токене недопустимые символы — нужны только латиница и цифры",
                            auth_state="rejected")
    line = connection_line_text(bad_token).plain
    assert "недопустимые символы" in line and "сервер недоступен" not in line


def test_an_inconclusive_answer_keeps_its_reason():
    """Свой сбой и не-вердиктный ответ статуса не теряют причину: сервер жив, а
    сказать про него нечего — раньше и то и другое печаталось как «недоступен»."""
    r = ProbeResult("unknown", 500, None, 10, "сервер ответил 500")
    assert connection_line_text(r).plain == "? не удалось проверить: сервер ответил 500"
    internal = ProbeResult("unknown", None, None, 0, "внутренняя ошибка проверки: boom")
    line = connection_line_text(internal).plain
    assert "внутренняя ошибка" in line and "сервер недоступен" not in line
    canary = ProbeResult("unknown", 400, None, 9,
                         "не удалось подтвердить токен: на заведомо неверный сервер "
                         "отвечает так же (400)")
    line = connection_line_text(canary).plain
    assert "подтвердить токен" in line and "сервер недоступен" not in line


# ── save_warning: реплика рядом с «сохранено», когда проба закончилась не «ок» ──
#
# Читает смысл из auth_state/offline, а не из verdict: «rejected» бывает и про имя
# модели, «unknown» — вердиктом канарейки при живом сервере (разбор ревью 2026-08-18).

def test_save_warning_is_silent_when_there_is_nothing_to_say():
    from tui.services.probe_presentation import save_warning
    assert save_warning(None) == ""
    assert save_warning(ProbeResult("ok", 200, None, 42, "доступен")) == ""


def test_save_warning_blames_the_token_when_the_token_was_judged():
    from tui.services.probe_presentation import save_warning
    r = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)", auth_state="rejected")
    assert save_warning(r) == "сервер отклонил токен — проверьте"


def test_save_warning_names_the_missing_token_not_a_rejected_one():
    """Токена не было вовсе: сервер отклонил контрольный запрос без него. Отклонять
    было нечего — «сервер отклонил токен» здесь неправда."""
    from tui.services.probe_presentation import save_warning
    r = ProbeResult("rejected", 401, None, 5, "сервер требует авторизацию (401)",
                    auth_state="required")
    assert "отклонил" not in save_warning(r)
    assert "требует авторизацию" in save_warning(r)


def test_save_warning_does_not_call_a_model_404_a_token_problem():
    """Живой GigaCode на неизвестную модель отвечает 404: вердикт «rejected», но он
    про имя модели. Прежний текст называл это отклонённым токеном."""
    from tui.services.probe_presentation import save_warning
    r = ProbeResult("rejected", 404, None, 5,
                    "отклонено сервером (404): проверьте адрес и имя модели")
    text = save_warning(r)
    assert "токен" not in text
    assert "проверьте адрес и имя модели" in text


def test_save_warning_does_not_call_a_live_server_unreachable():
    """Канарейка: сервер ОТВЕТИЛ, просто так же, как на заведомо неверный токен —
    вердикт «unknown» при живом сервере. Прежний текст объявлял его недоступным."""
    from tui.services.probe_presentation import save_warning
    r = ProbeResult("unknown", 400, None, 5,
                    "не удалось подтвердить токен: на заведомо неверный сервер "
                    "отвечает так же (400)")
    text = save_warning(r)
    assert "недоступен" not in text
    assert "заведомо неверный" in text


def test_save_warning_blames_the_server_only_when_it_did_not_answer():
    from tui.services.probe_presentation import save_warning
    r = ProbeResult("unknown", None, None, 5, "нет соединения (timeout)", offline=True)
    assert save_warning(r) == "проверить не удалось — сервер недоступен"


# ── token_accepted: единственное место, где из результата достают вердикт О ТОКЕНЕ ──

def test_token_accepted_trusts_the_differential_verdict():
    from tui.services.probe_presentation import token_accepted
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
    from tui.services.probe_presentation import token_accepted
    from model_picker import ModelInfo
    r = ProbeResult("ok", 200, [ModelInfo(id="m")], 10, "доступен")
    assert token_accepted(r, has_token=True) is None


def test_token_accepted_reads_the_real_route_without_the_differential():
    """Моделей в результате нет — значит отвечал сам chat/completions (проба уходит
    туда фолбэком). Не-401 там означает, что запрос прошёл авторизацию и спорит уже
    о теле: живой GigaCode на неверное имя модели отвечает 404 при рабочем токене."""
    from tui.services.probe_presentation import token_accepted
    r = ProbeResult("rejected", 404, None, 12,
                    "отклонено сервером (404): проверьте адрес и имя модели")
    assert token_accepted(r, has_token=True) is True


def test_token_accepted_reads_a_401_without_the_differential_too():
    """401 — отказ на самом маршруте: достоверно и без differential."""
    from tui.services.probe_presentation import token_accepted
    r = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")
    assert token_accepted(r, has_token=True) is False


def test_token_accepted_when_there_is_no_token_at_all():
    """Токена нет — значит запрос по реальному маршруту и был контрольным: ответил
    сервер осмысленно, авторизация не требуется; 401 — требуется, а предъявить
    нечего; иное — утверждать не о чем. Ответ справочника и тут не в счёт: он
    отвечает без токена всем, а chat/completions может его требовать."""
    from tui.services.probe_presentation import token_accepted
    from model_picker import ModelInfo
    ok = ProbeResult("ok", 200, None, 10, "доступен")
    needs = ProbeResult("rejected", 401, None, 5, "токен отклонён (401)")
    weird = ProbeResult("unknown", 500, None, 5, "сервер ответил 500")
    catalog = ProbeResult("ok", 200, [ModelInfo(id="m")], 10, "доступен")
    assert token_accepted(ok, has_token=False) is True
    assert token_accepted(needs, has_token=False) is False
    assert token_accepted(weird, has_token=False) is None
    assert token_accepted(catalog, has_token=False) is None


def test_token_accepted_says_nothing_when_the_probe_never_got_a_verdict():
    """Вердикта нет («unknown»): сервер не ответил или канарейка не различила наш
    токен от мусорного. Токен на руках — не довод: фолбэк «раз токен есть, значит
    принят» красил выключенный сервер в зелёное «токен принят». Ответ тот же, что
    и в ветке «токена нет вовсе», — сказать нечего."""
    from tui.services.probe_presentation import token_accepted
    offline = ProbeResult("unknown", None, None, 0, "нет соединения", offline=True)
    canary = ProbeResult("unknown", 400, None, 8, "сервер отвечает, но токен не различает",
                         body_signature="no-connected-db")
    assert token_accepted(offline, has_token=True) is None
    assert token_accepted(canary, has_token=True) is None
