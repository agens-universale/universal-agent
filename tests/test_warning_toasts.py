"""warning_toasts: выбор warning-уведомлений для тоста при старте (кап + сводный)."""
from startup_notices import Notice, warning_toasts


def test_caps_at_three_and_summarizes_rest():
    notices = [Notice("warning", f"w{i}", f"msg{i}") for i in range(5)] + [Notice("info", "i", "info")]
    msgs, summary, codes = warning_toasts(notices)
    assert msgs == ["msg0", "msg1", "msg2"]                 # кап 3
    assert summary == "+2 ещё · /notices (F4)"
    assert codes == frozenset(f"w{i}" for i in range(5))    # все warning-коды (для on-change)


def test_no_summary_when_within_cap():
    notices = [Notice("warning", "w1", "m1"), Notice("info", "i", "x")]
    msgs, summary, codes = warning_toasts(notices)
    assert msgs == ["m1"] and summary is None and codes == frozenset({"w1"})


def test_empty_when_no_warnings():
    msgs, summary, codes = warning_toasts([Notice("info", "i", "x")])
    assert msgs == [] and summary is None and codes == frozenset()


def test_info_never_toasted():
    notices = [Notice("info", "i1", "a"), Notice("info", "i2", "b"), Notice("warning", "w", "c")]
    msgs, summary, codes = warning_toasts(notices)
    assert msgs == ["c"] and codes == frozenset({"w"})       # только warning
