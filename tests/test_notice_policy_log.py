import logging

from gc_core.message_policy.notice_policy_log import log_notice_policy_step


def test_log_notice_policy_step_writes_counters(caplog):
    caplog.set_level(logging.INFO)
    log = logging.getLogger("test.notice_policy")
    log_notice_policy_step(
        log,
        tool_counters={"session_memory": 7, "exec_fetch_url": 2},
        tool_thresholds={"session_memory": 8, "exec_fetch_url": 5},
        tool_resets={"session_memory": "incremented"},
        tool_emits=[],
        long_check_cnt=3,
        long_interval=4,
        long_reset=False,
        long_emit=False,
    )
    assert len(caplog.records) == 1
    message = caplog.records[0].message
    assert "[notice_policy]" in message
    assert "session_memory=7/8" in message
    assert "exec_fetch_url=2/5" in message
    assert "project_memory_check_cnt=3/4" in message
    assert "reset tool_usage_notice[session_memory]" not in message
    assert "tool_usage_notice[session_memory] incremented" in message
