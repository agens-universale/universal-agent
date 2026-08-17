"""_pending_wire_match: реконсиляция pending-реплики с polled .jsonl-записью.

Polled-контент приходит из map_message уже .strip()'нутым, а wire в кэше может
иметь хвостовой/ведущий whitespace (многострочный промпт /init из файла-шаблона
кончается на \\n). Матч должен это терпеть — иначе оптимистичный маркер не
схлопывается и полный промпт течёт в трейс отдельным user-сообщением."""
from gc_tui.screens.main import TraceScreen


def test_wire_match_exact():
    scr = TraceScreen()
    scr._pending_wire_cache["c1"] = "hello"
    assert scr._pending_wire_match("c1", "hello")


def test_wire_match_ignores_trailing_whitespace():
    """wire с хвостовым \\n (как init-промпт) ↔ polled stripped → матч."""
    scr = TraceScreen()
    scr._pending_wire_cache["c1"] = "PROMPT BODY\n"
    assert scr._pending_wire_match("c1", "PROMPT BODY")


def test_wire_match_negative_when_content_differs():
    scr = TraceScreen()
    scr._pending_wire_cache["c1"] = "hello"
    assert not scr._pending_wire_match("c1", "goodbye")
