from gc_core import onboarding
from gc_cli import onboarding_wizard as wz


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

    monkeypatch.setattr(wz.onboarding_config_writer, "write_values", failing_write_values)
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
