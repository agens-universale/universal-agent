"""Структурная валидация endpoint в плоском визарде.
Проверка: схема http(s) + непустой хост. Путь/достижимость НЕ проверяем (осознанно)."""

from gc_cli import onboarding_wizard as wz
from gc_core import onboarding


def _io(reads):
    it = iter(reads)
    printed = []
    io = wz.WizardIO(reader=lambda p, s: next(it, ""), printer=lambda m: printed.append(m))
    return io, printed


def test_valid_endpoint_url_accepts_http_https():
    assert wz._valid_endpoint_url("https://host/v1/chat/completions")
    assert wz._valid_endpoint_url("http://46.243.227.147:15000/v1/chat/completions")


def test_valid_endpoint_url_rejects_malformed():
    assert not wz._valid_endpoint_url("not-a-url")  # нет схемы
    assert not wz._valid_endpoint_url("http://")  # схема есть, хоста нет
    assert not wz._valid_endpoint_url("ftp://host/x")  # не http(s)
    assert not wz._valid_endpoint_url("")


def test_valid_endpoint_url_allows_truncated_path():
    # осознанный компромисс «только структурная»: обрезанный путь структурно валиден → проходит
    assert wz._valid_endpoint_url("http://46.243.227.147:15000/v1/chat/comple")


def test_invalid_endpoint_reprompts_until_valid():
    # опечатка «ttp://…» → предупреждение + переспрос → валидный ввод принимается
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    io, printed = _io(
        [
            "ttp://46.243.227.147:15000/v1/chat/completions",  # опечатка
            "https://46.243.227.147:15000/v1/chat/completions",
        ]
    )  # валидный
    values = wz.collect_fields(io, cfg, review_all=False)
    assert values["completions_url"] == "https://46.243.227.147:15000/v1/chat/completions"
    assert any("не похоже на URL" in l for l in printed)


def test_invalid_endpoint_then_enter_skips():
    # невалид → Enter = явный пропуск (поле остаётся pending), НЕ застревает
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    io, _ = _io(["garbage-no-scheme", ""])  # невалид, затем Enter
    values = wz.collect_fields(io, cfg, review_all=False)
    assert "completions_url" not in values


def test_review_all_hides_current_for_placeholder_endpoint(monkeypatch):
    # дефолтный плейсхолдер (== URL_SENTINEL) НЕ показываем как «текущее»: он = «не задано»
    from gc_core import model_picker

    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **k: None)
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    io, printed = _io(["", "3", ""])  # endpoint skip · auth «3» · model ручной skip
    wz.collect_fields(io, cfg, review_all=True)
    assert not any("[текущее" in l for l in printed)


def test_endpoint_prompt_uses_flat_indent_ascii_marker():
    # единый стиль: endpoint-приглашение = «  > » (2 отступа, ASCII '>'), без старого '›'
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    prompts = []
    io = wz.WizardIO(reader=lambda p, s: prompts.append(p) or "", printer=lambda m: None)
    wz.collect_fields(io, cfg, review_all=False)
    assert "  > " in prompts  # endpoint: плоский 2-отступ + '>'
    assert not any("›" in p for p in prompts)  # старый маркер убран везде


def test_fetch_models_does_not_dial_the_placeholder_endpoint():
    """Заглушка адреса — «человек ничего не задал», и стучаться по ней нельзя: запрос
    уходил на несуществующий `your-backend:PORT` и ждал таймаута, чтобы напечатать
    «список не получен». Шаг обязан вести себя как на чистой установке — без адреса."""
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
    }
    dialed = []

    def fetch(endpoint, auth, **kw):
        dialed.append(endpoint)
        from gc_core.model_picker import ModelInfo

        return [ModelInfo(id="m")]

    assert wz._fetch_models(cfg, {}, fetch) is None
    assert dialed == []


def test_fetch_models_preserves_custom_ca_path():
    from unittest.mock import Mock

    fetch = Mock(return_value=[])
    cfg = {"completions_url": "https://model.example/v1/chat/completions", "use_ssl": "/corp/root.pem"}
    assert wz._fetch_models(cfg, {}, fetch) == []
    assert fetch.call_args.kwargs["verify"] == "/corp/root.pem"


def test_model_step_on_placeholder_endpoint_offers_manual_input():
    """…и тем же текстом, что при отсутствующем адресе (`MODEL_NO_LIST` + свободный
    ввод): своего сообщения про заглушку у шага нет и заводить его незачем."""
    from gc_core import onboarding_fields as _of
    from gc_core.model_picker import ModelInfo

    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": onboarding.MODEL_SENTINEL,
    }
    io, printed = _io(["gpt-5.6-terra"])
    values = {}
    wz._collect_model(io, cfg, values, fetch=lambda *a, **k: [ModelInfo(id="glm")])
    assert any(_of.MODEL_NO_LIST in line for line in printed)
    assert values["model"] == "gpt-5.6-terra"


def test_review_all_shows_current_for_real_endpoint(monkeypatch):
    # реально настроенный адрес — показываем «текущее»
    from gc_core import model_picker

    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **k: None)
    cfg = {
        "completions_url": "https://real/v1/chat/completions",
        "chat_completion_headers": {"Authorization": "Bearer sk"},
        "model": "m",
    }
    io, printed = _io(["", "3", ""])
    wz.collect_fields(io, cfg, review_all=True)
    assert any("[текущее: https://real/v1/chat/completions]" in l for l in printed)


def test_review_all_does_not_show_placeholder_as_current(monkeypatch):
    # Самодостаточный реестр закрывает требование адреса (_registry_is_self_sufficient),
    # поэтому completions_url не pending — и витрина «текущее» доходит до печати с
    # плейсхолдером шаблона в руках. Заглушка не значение: показать её как
    # действующий адрес значит соврать человеку о том, куда пойдёт запрос.
    from gc_core import model_picker

    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **k: None)
    cfg = {
        "backend_api_mode": "chat_completions",
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "models": {
            "own": {"model": "x", "url": "https://own/v1/chat/completions", "authorization_header": "Bearer own"}
        },
    }
    assert "completions_url" not in onboarding.pending_field_keys(cfg)  # стенд верен
    io, printed = _io(["", "3", ""])
    wz.collect_fields(io, cfg, review_all=True)
    assert not any("текущее" in line for line in printed), printed
