from gc_cli import onboarding_wizard as wz
from gc_core import onboarding


def _io(answers):
    it = iter(answers)
    return wz.WizardIO(reader=lambda p, secret: next(it), printer=lambda m: None)


def test_auth_menu_token():
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
           "model": "m"}
    # endpoint пропущен(Enter), auth: выбор "1", токен "sk-1"
    io = _io(["", "1", "sk-1"])
    values = wz.collect_fields(io, cfg, review_all=False)
    assert values["Authorization"] == "Bearer sk-1"


def test_auth_menu_no_auth_writes_empty():
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
           "model": "m"}
    io = _io(["", "2"])   # endpoint пропущен, auth: "2" без авторизации
    values = wz.collect_fields(io, cfg, review_all=False)
    assert values["Authorization"] == ""


def test_auth_menu_skip():
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
           "model": "m"}
    io = _io(["", "3"])   # endpoint пропущен, auth: "3" пропустить
    values = wz.collect_fields(io, cfg, review_all=False)
    assert "Authorization" not in values


def test_auth_token_prompt_flat_style():
    # ввод токена — общий стиль: хинт «  токен (ввод скрыт)» + приглашение «  > » (секретное)
    cfg = {"completions_url": onboarding.URL_SENTINEL,
           "chat_completion_headers": {"Authorization": onboarding.TOKEN_SENTINEL},
           "model": "m"}
    prompts, out = [], []
    seq = iter(["", "1", "sk-1"])   # endpoint skip, auth «1», токен
    io = wz.WizardIO(reader=lambda p, s: (prompts.append((p, s)), next(seq))[1], printer=out.append)
    wz.collect_fields(io, cfg, review_all=False)
    assert ("  > ", True) in prompts                          # секретный ввод токена = «  > »
    assert any(l.strip() == "токен (ввод скрыт)" for l in out)
    assert not any("токен (ввод скрыт): " in p for p, _ in prompts)  # старое labeled убрано
