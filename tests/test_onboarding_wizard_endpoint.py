"""Структурная валидация endpoint в плоском визарде.
Проверка: схема http(s) + непустой хост. Путь/достижимость НЕ проверяем (осознанно)."""
import onboarding
import onboarding_wizard as wz


def _io(reads):
    it = iter(reads)
    printed = []
    io = wz.WizardIO(reader=lambda p, s: next(it, ""), printer=lambda m: printed.append(m))
    return io, printed


def test_valid_endpoint_url_accepts_http_https():
    assert wz._valid_endpoint_url("https://host/v1/chat/completions")
    assert wz._valid_endpoint_url("http://46.243.227.147:15000/v1/chat/completions")


def test_valid_endpoint_url_rejects_malformed():
    assert not wz._valid_endpoint_url("not-a-url")     # нет схемы
    assert not wz._valid_endpoint_url("http://")       # схема есть, хоста нет
    assert not wz._valid_endpoint_url("ftp://host/x")  # не http(s)
    assert not wz._valid_endpoint_url("")


def test_valid_endpoint_url_allows_truncated_path():
    # осознанный компромисс «только структурная»: обрезанный путь структурно валиден → проходит
    assert wz._valid_endpoint_url("http://46.243.227.147:15000/v1/chat/comple")


def test_invalid_endpoint_reprompts_until_valid():
    # опечатка «ttp://…» → предупреждение + переспрос → валидный ввод принимается
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    io, printed = _io(["ttp://46.243.227.147:15000/v1/chat/completions",   # опечатка
                       "https://46.243.227.147:15000/v1/chat/completions"])  # валидный
    values = wz.collect_fields(io, cfg, review_all=False)
    assert values["completions_url"] == "https://46.243.227.147:15000/v1/chat/completions"
    assert any("не похоже на URL" in l for l in printed)


def test_invalid_endpoint_then_enter_skips():
    # невалид → Enter = явный пропуск (поле остаётся pending), НЕ застревает
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    io, _ = _io(["garbage-no-scheme", ""])    # невалид, затем Enter
    values = wz.collect_fields(io, cfg, review_all=False)
    assert "completions_url" not in values


def test_review_all_hides_current_for_placeholder_endpoint(monkeypatch):
    # дефолтный плейсхолдер (== URL_SENTINEL) НЕ показываем как «текущее»: он = «не задано»
    import model_picker
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **k: None)
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    io, printed = _io(["", "3", ""])          # endpoint skip · auth «3» · model ручной skip
    wz.collect_fields(io, cfg, review_all=True)
    assert not any("[текущее" in l for l in printed)


def test_endpoint_prompt_uses_flat_indent_ascii_marker():
    # единый стиль: endpoint-приглашение = «  > » (2 отступа, ASCII '>'), без старого '›'
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    prompts = []
    io = wz.WizardIO(reader=lambda p, s: prompts.append(p) or "", printer=lambda m: None)
    wz.collect_fields(io, cfg, review_all=False)
    assert "  > " in prompts                    # endpoint: плоский 2-отступ + '>'
    assert not any("›" in p for p in prompts)   # старый маркер убран везде


def test_review_all_shows_current_for_real_endpoint(monkeypatch):
    # реально настроенный адрес — показываем «текущее»
    import model_picker
    monkeypatch.setattr(model_picker, "fetch_models", lambda *a, **k: None)
    cfg = {"completions_url": "https://real/v1/chat/completions",
           "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"}
    io, printed = _io(["", "3", ""])
    wz.collect_fields(io, cfg, review_all=True)
    assert any("[текущее: https://real/v1/chat/completions]" in l for l in printed)
