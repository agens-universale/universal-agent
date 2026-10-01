"""AgentRunner: картинки в аргументах хода и отдельная очередь ответов на turn_ask_human."""

from gc_core.tools.turn.ask_human import HumanAnswer

from gc_tui.services.agent_runner import AgentRunner
from image_helpers import make_attachment_file


class _Recorder:
    """Executor, запоминающий аргументы arun."""

    def __init__(self):
        self.calls: list[dict] = []

    async def arun(self, messages, **kw):
        self.calls.append({"messages": list(messages), **kw})

    def stop_run(self):
        pass

    def add_message(self, m):
        pass


def test_run_args_put_images_next_to_the_query_and_none_next_to_the_technical_tail(
    tmp_path,
):
    att = make_attachment_file(1, tmp_path)
    args = AgentRunner._run_args(
        query="Q",
        technical="<technical>x</technical>",
        message_id="m1",
        display_query=None,
        skill_loaded=None,
        attachments=(att,),
    )
    assert args["attachments"] == [[att], None]


def test_run_args_without_images_give_a_none_slot():
    args = AgentRunner._run_args(
        query="Q",
        technical=None,
        message_id=None,
        display_query=None,
        skill_loaded=None,
    )
    assert args["attachments"] == [None]


def test_start_passes_attachments_to_arun(tmp_path):
    att = make_attachment_file(2, tmp_path)
    ex = _Recorder()
    runner = AgentRunner(executor=ex)
    runner.start(query="Q", thread_id="t1", attachments=(att,))
    runner.wait(timeout=2.0)
    assert ex.calls[0]["attachments"] == [[att]]


def test_human_answer_reaches_the_port_as_is(tmp_path):
    att = make_attachment_file(3, tmp_path)
    runner = AgentRunner(executor=_Recorder())
    answer = HumanAnswer(text="[Изображение #3] вот", attachments=(att,))
    runner.submit_ask_human_reply(answer)
    assert runner._read_answer_blocking("?") is answer


def test_text_answer_reaches_the_port_stripped():
    runner = AgentRunner(executor=_Recorder())
    runner.submit_ask_human_reply("  да\nи ещё строка  ")
    assert runner._read_answer_blocking("?") == "да\nи ещё строка"


def test_cancel_gives_the_port_an_empty_answer():
    runner = AgentRunner(executor=_Recorder())
    runner.cancel_ask_human()
    assert runner._read_answer_blocking("?") == ""


def test_answers_do_not_go_through_the_stdin_bridge():
    """Мост redirect_stdin — для чужого input(); ответ человека в него не попадает,
    иначе чужой input() съел бы ответ (дизайн картинок §20.3)."""
    runner = AgentRunner(executor=_Recorder())
    runner.submit_ask_human_reply("ответ")
    assert runner._stdin_queue.empty()
