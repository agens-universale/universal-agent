from gc_core.agent_executor import AgentExecutor


def test_display_content_set_when_differs():
    fn = AgentExecutor._build_user_messages
    msgs = fn(
        ["<skill>\nдавай"], message_ids=["m1"], display_contents=["давай"], skills_loaded=["qa-x"], gen_id=lambda p: "g"
    )
    assert msgs[0]["content"] == "<skill>\nдавай"
    assert msgs[0]["display_content"] == "давай"
    assert msgs[0]["skill_loaded"] == "qa-x"


def test_no_fields_for_plain_message():
    fn = AgentExecutor._build_user_messages
    msgs = fn(["обычный запрос"], message_ids=None, display_contents=None, skills_loaded=None, gen_id=lambda p: "g")
    assert msgs[0] == {"role": "user", "content": "обычный запрос", "id": "g"}


def test_display_not_set_when_equal_to_content():
    fn = AgentExecutor._build_user_messages
    msgs = fn(["текст"], message_ids=None, display_contents=["текст"], skills_loaded=None, gen_id=lambda p: "g")
    assert "display_content" not in msgs[0]


def test_empty_display_content_is_set():
    # "" отличается от wire → ставим (mid-run доставка)
    fn = AgentExecutor._build_user_messages
    msgs = fn(["<skill>"], message_ids=None, display_contents=[""], skills_loaded=["qa-x"], gen_id=lambda p: "g")
    assert msgs[0]["display_content"] == ""
    assert msgs[0]["skill_loaded"] == "qa-x"
