import json

from tools.session.answer_question import AnswerQuestion


def test_answer_question_success(capsys):
    out = json.loads(AnswerQuestion()(answer="hello"))
    assert out["ok"] is True
    assert out["answer"] == "hello"
    assert out["error"] is None
    captured = capsys.readouterr()
    assert "hello" in captured.err


def test_answer_question_errors():
    tool = AnswerQuestion()
    bad_type = json.loads(tool(answer=1))  # type: ignore[arg-type]
    assert bad_type["ok"] is False
    assert bad_type["error"]["field"] == "answer"
    assert "must be a string" in bad_type["error"]["message"]

    empty = json.loads(tool(answer="   "))
    assert empty["ok"] is False
    assert empty["error"]["field"] == "answer"
    assert "cannot be empty" in empty["error"]["message"]


def test_answer_question_empty_artifact_path_treated_as_omitted():
    out = json.loads(AnswerQuestion()(answer="ok", artifact_path=""))
    assert out["ok"] is True
    assert out["answer"] == "ok"
    assert "artifact_path" not in out


def test_answer_question_with_artifact_path(capsys):
    out = json.loads(
        AnswerQuestion()(answer="Короткий ответ. Подробности в артефакте.", artifact_path="report.md")
    )
    assert out["ok"] is True
    assert out["artifact_path"] == "report.md"
    captured = capsys.readouterr()
    assert "report.md" in captured.err
