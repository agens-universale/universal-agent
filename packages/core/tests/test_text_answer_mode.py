from types import SimpleNamespace

from gc_core.intent_guard.text_answer_judge import should_return_text_answer


def _ctx(mode):
    return SimpleNamespace(config_dict={"text_answer_mode": mode})


def test_allow_accepts_text():
    assert should_return_text_answer(_ctx("allow"), object(), "hi").accept is True


def test_deny_bounces_text():
    assert should_return_text_answer(_ctx("deny"), object(), "hi").accept is False


def test_empty_text_always_bounces():
    assert should_return_text_answer(_ctx("allow"), object(), "   ").accept is False
