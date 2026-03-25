from tools.simple_tools.deprecated_tools.answer_question import AnswerQuestion


def test_answer_question_success(capsys):
    out = AnswerQuestion()(answer="hello")
    assert out == "Answer delivered to user"
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_answer_question_errors():
    tool = AnswerQuestion()
    assert "must be a string" in tool(answer=1)  # type: ignore[arg-type]
    assert "cannot be empty" in tool(answer="   ")
