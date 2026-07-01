def test_run_args_builds_parallel_lists():
    from tui.services.agent_runner import AgentRunner
    args = AgentRunner._run_args(query="WIRE", technical=None, message_id="m1",
                                 display_query="чисто", skill_loaded="qa-x")
    assert args["msgs"] == ["WIRE"]
    assert args["display_contents"] == ["чисто"]
    assert args["skills_loaded"] == ["qa-x"]
    assert args["message_ids"] == ["m1"]


def test_run_args_technical_tail_gets_none():
    from tui.services.agent_runner import AgentRunner
    args = AgentRunner._run_args(query="WIRE", technical="<technical>x</technical>",
                                 message_id="m1", display_query="чисто", skill_loaded=None)
    assert args["msgs"] == ["WIRE", "<technical>x</technical>"]
    assert args["display_contents"] == ["чисто", None]
    assert args["skills_loaded"] == [None, None]
    assert args["message_ids"] == ["m1", None]
