import onboarding_wizard as wz


def _capture():
    lines = []
    io = wz.WizardIO(reader=lambda p, s: "", printer=lambda m: lines.append(m))
    return io, lines


def test_rule_header_symmetric_and_enter_indent():
    # линия симметрична (по 3 «─» с боков), Enter — на отступе 2 (наравне с блоками)
    lines = []
    io = wz.WizardIO(reader=lambda p, s: "", printer=lines.append)
    wz._rule_header(io)
    assert lines == [
        "",
        "─── Настройка GigaCode Universal Agent ───",
        "",                                            # пустая строка между линией и Enter
        "  Enter — пропустить · Ctrl-C — выйти",
    ]


def test_run_prints_rule_header_and_final_check(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n  completions_url: \"https://x/v1/chat/completions\"\n"
        "  chat_completion_headers:\n    Authorization: \"Bearer sk\"\n"
        "agent:\n  model: \"m\"\n", encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1", "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"})
    io, lines = _capture()
    # уже сконфигурен → collect_fields ничего не спросит (не review_all, pending пуст)
    wz.run({"completions_url": "https://x/v1",
            "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"},
           cfg, review_all=False, io=io)
    blob = "\n".join(lines)
    assert lines[0] == ""                                     # пустая строка перед хедером
    assert "─── Настройка GigaCode Universal Agent" in blob   # rule-хедер
    idx = next(i for i, l in enumerate(lines) if l.strip().startswith("✓ Готово"))
    assert lines[idx - 1] == ""                               # пустая строка перед финалом
    assert lines[-1] == ""                                    # и после (воздух перед промптом)
    # После «✓ Готово» идут 3 строки сводки (три уровня) + пустая строка
    assert idx == len(lines) - 5


def test_endpoint_rendered_as_section(tmp_path, monkeypatch):
    # конфиг с плейсхолдером endpoint → поле endpoint pending → визард спросит его секцией
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n  completions_url: \"http://your-backend:PORT/v1/chat/completions\"\n"
        "  chat_completion_headers:\n    Authorization: \"Bearer sk\"\n"
        "agent:\n  model: \"m\"\n", encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1", "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"})
    printed = []
    it = iter(["https://real/v1/chat/completions"])   # ввод endpoint
    io = wz.WizardIO(reader=lambda p, s: next(it, ""), printer=lambda m: printed.append(m))
    wz.run({"completions_url": "http://your-backend:PORT/v1/chat/completions",
            "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"},
           cfg, review_all=False, io=io)
    # «Адрес сервера» напечатан ОТДЕЛЬНОЙ строкой (заголовок секции), а не вклеен в prompt ввода
    assert any(l.strip() == "Адрес сервера" for l in printed)


def test_blank_line_separates_blocks(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yml"
    cfg.write_text(
        "backend:\n  completions_url: \"https://x/v1/chat/completions\"\n"
        "  chat_completion_headers:\n    Authorization: \"Bearer sk\"\n"
        "agent:\n  model: \"m\"\n", encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1", "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"})
    # Список моделей не участвует в проверке — без подмены шаг «Модель» реально
    # стучится на «https://x/v1» (зелёный тест сейчас — только потому, что DNS
    # не резолвит этот хост).
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    io, lines = _capture()
    wz.run({"completions_url": "https://x/v1",
            "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"},
           cfg, review_all=True, io=io)   # review_all → спросит все три секции
    # хотя бы одна пустая строка-разделитель напечатана
    assert "" in lines
