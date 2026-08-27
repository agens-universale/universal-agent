import yaml

import onboarding
import onboarding_config_writer
import onboarding_wizard as wz


class _FakeMcp:
    def __init__(self):
        self.installed = []
    def installable_names(self):
        return ["office", "science", "duckduckgo"]
    def install(self, name=None):
        self.installed.append(name)
        return 0


def _io(answers):
    it = iter(answers)
    return wz.WizardIO(reader=lambda p, secret: next(it), printer=lambda m: None)


def test_run_writes_fields_installs_selected_and_returns_configured(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n"
        '  model: "gigacode_agent_v1.6_rc"\n',
        encoding="utf-8",
    )
    # После записи load_config должен вернуть «настроено».
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1",
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": "gigacode_agent_v1.6_rc",
    })
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    mcp = _FakeMcp()
    # endpoint, auth-меню «1» (ввести токен), токен, MCP-меню «2» (выбрать), номера «1,3»
    io = _io(["https://x/v1", "1", "sk-1", "2", "1,3"])
    configured = wz.run(cfg, cfg_file, review_all=False, io=io, mcp=mcp)
    assert configured is True
    text = cfg_file.read_text(encoding="utf-8")
    assert '  completions_url: "https://x/v1"' in text
    assert '    Authorization: "Bearer sk-1"' in text
    assert mcp.installed == ["office", "duckduckgo"]


def test_run_returns_false_when_still_pending(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n"
        '  model: "gigacode_agent_v1.6_rc"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": onboarding.URL_SENTINEL,   # так и не задан
        "chat_completion_headers": {"Authorization": "Bearer sk-1"},
        "model": "gigacode_agent_v1.6_rc",
    })
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
        "model": "gigacode_agent_v1.6_rc",
    }
    # endpoint пропущен (Enter), auth-меню «1» (ввести токен), токен, MCP пропустить «3»
    io = _io(["", "1", "sk-1", "3"])
    assert wz.run(cfg, cfg_file, review_all=False, io=io, mcp=_FakeMcp()) is False


def test_run_without_mcp_skips_mcp(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n  model: \"m\"\n", encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": "https://x/v1", "chat_completion_headers": {"Authorization": "Bearer sk"}, "model": "m"})
    it = iter(["", "1", "sk"])   # endpoint skip, auth "1" token, token
    io = wz.WizardIO(reader=lambda p, s: next(it), printer=lambda m: None)
    # mcp не передаём → шаг MCP не должен вызваться (нет исключения от отсутствующего mcp)
    assert wz.run({"completions_url": "http://your-backend:PORT/v1/chat/completions",
                   "chat_completion_headers": {"Authorization": "Bearer <ВПИШИТЕ_ТОКЕН>"}, "model": "m"},
                  cfg_file, review_all=False, io=io) is True


def test_run_writes_registry_entry_and_flat_connection(tmp_path, monkeypatch):
    """CLI-визард пишет ТО ЖЕ, что и TUI-мастер (ядро записи одно, onboarding.py):
    подключение — плоскими ключами, модель — записью реестра со слотом default;
    легаси `agent.model` не трогается, конфиг после прогона настроен."""
    import config as cfg_mod
    from bundled_assets import bundled_root
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(cfg_mod, "_project_local_config_path", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)   # без сети
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: cfg_mod.load_config(str(cfg_file)))

    starting = cfg_mod.load_config(str(cfg_file))
    # адрес, auth-меню «1» (токен), токен, модель (списка нет → ручной ввод)
    io = _io(["https://x/v1/chat/completions", "1", "sk-1", "qwen/qwen3-coder"])
    assert wz.run(starting, cfg_file, review_all=False, io=io, mcp=None) is True

    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["backend"]["completions_url"] == "https://x/v1/chat/completions"
    assert raw["backend"]["chat_completion_headers"]["Authorization"] == "Bearer sk-1"
    assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
    assert raw["slots"]["default"] == "qwen3-coder"
    assert raw["agent"]["model"] == onboarding.MODEL_SENTINEL
    assert onboarding.is_unconfigured(cfg_mod.load_config(str(cfg_file))) is False


def test_run_with_registry_but_skipped_endpoint_stays_unconfigured(tmp_path, monkeypatch):
    """Модель записана в реестр, а адрес пропущен Enter'ом: визард обязан отчитаться
    «не настроено» и напечатать гайд. Иначе «✓ Готово» закрывало бы вопрос навсегда —
    следующий старт мастер не предложил бы, а запрос ушёл бы на плейсхолдерный хост."""
    import config as cfg_mod
    from bundled_assets import bundled_root
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(cfg_mod, "_project_local_config_path", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: cfg_mod.load_config(str(cfg_file)))

    printed = []
    it = iter(["", "3", "qwen3-coder"])   # адрес пропущен, auth-меню «3» (пропустить), модель
    io = wz.WizardIO(reader=lambda p, s: next(it), printer=printed.append)
    assert wz.run(cfg_mod.load_config(str(cfg_file)), cfg_file,
                  review_all=False, io=io, mcp=None) is False

    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["models"] == {"qwen3-coder": {"model": "qwen3-coder"}}    # модель записана
    assert raw["backend"]["completions_url"] == onboarding.URL_SENTINEL  # адрес так и не задан
    blob = "\n".join(printed)
    assert "настройка не завершена" in blob
    assert "backend.completions_url" in blob


def test_run_reports_validation_error_without_traceback(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "backend:\n"
        '  completions_url: "http://your-backend:PORT/v1/chat/completions"\n'
        "  chat_completion_headers:\n"
        '    Authorization: "Bearer <ВПИШИТЕ_ТОКЕН>"\n'
        "agent:\n  model: \"m\"\n", encoding="utf-8")

    # write_values бросает ValueError → run() обрабатывает без трейсбека
    def failing_write_values(*args, **kwargs):
        raise ValueError("схема не валидна")

    monkeypatch.setattr(onboarding_config_writer, "write_values", failing_write_values)
    monkeypatch.setattr(wz, "_reload_config", lambda: {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real"},
        "model": "m",
    })

    printed = []
    # Pending — ТОЛЬКО endpoint (auth и модель заданы): reader отвечает один раз,
    # write_values бросает ValueError → run() обрабатывает без трейсбека.
    io = wz.WizardIO(reader=lambda p, s: "https://x/v1", printer=lambda m: printed.append(m))
    cfg = {
        "completions_url": onboarding.URL_SENTINEL,
        "chat_completion_headers": {"Authorization": "Bearer real"},
        "model": "m",
    }
    wz.run(cfg, cfg_file, review_all=False, io=io)

    # run() не бросает; printer получает сообщение об ошибке
    output = "\n".join(printed)
    assert "конфиг не записан" in output
    assert "схема не валидна" in output


def test_run_does_not_claim_an_untouched_config_after_the_registry_landed(
        tmp_path, monkeypatch):
    """Брат теста выше, второй полюс: там плоская запись — единственная, и «конфиг
    не записан» правда; здесь ей предшествует запись реестра, которая УЖЕ на диске.

    Записей две, и реестр идёт первым (политика в `onboarding.write_wizard_values`).
    Отказ второй оставляет модели в файле — сказать «конфиг не записан» значит
    отправить человека искать нетронутый файл, а там уже новый реестр."""
    import config as cfg_mod
    from bundled_assets import bundled_root
    from config_modules import env_vars

    monkeypatch.setattr(env_vars, "gc_paths", lambda: {
        "config": tmp_path / "gc", "state": tmp_path / "s",
        "data": tmp_path / "d", "cache": tmp_path / "c",
    })
    monkeypatch.setattr(cfg_mod, "_project_local_config_path", lambda: None)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("model_picker.fetch_models", lambda *a, **k: None)
    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        (bundled_root() / "config.default.yml").read_text(encoding="utf-8"),
        encoding="utf-8")
    monkeypatch.setattr(wz, "_reload_config", lambda: cfg_mod.load_config(str(cfg_file)))

    def failing_write_values(*args, **kwargs):
        raise ValueError("схема не валидна")

    monkeypatch.setattr(onboarding_config_writer, "write_values", failing_write_values)

    printed = []
    it = iter(["https://x/v1/chat/completions", "1", "sk-1", "qwen/qwen3-coder"])
    io = wz.WizardIO(reader=lambda p, s: next(it), printer=printed.append)
    wz.run(cfg_mod.load_config(str(cfg_file)), cfg_file, review_all=False, io=io, mcp=None)

    raw = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert raw["models"] == {"qwen3-coder": {"model": "qwen/qwen3-coder"}}
    output = "\n".join(printed)
    assert "схема не валидна" in output
    assert "конфиг не записан" not in output
    assert "модел" in output          # сказано, что записи моделей уже в файле
