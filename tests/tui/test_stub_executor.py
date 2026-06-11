"""StubExecutor генерирует детерминированную последовательность событий в data_*.jsonl."""
import json

from tests.tui.fixtures.stub_executor import StubExecutor


def test_stub_executor_writes_full_dialog(tmp_path):
    fake = StubExecutor(threads_dir=str(tmp_path), system="cli")
    fake.run(["расскажи об этом проекте"], thread_id="t1")

    data = (tmp_path / "data_cli_t1.jsonl").read_text(encoding="utf-8")
    lines = [json.loads(l) for l in data.strip().split("\n")]

    # Ожидаем: user, tool_call read_file, tool_output ok, tool_call read_file MISSING,
    # tool_output error, submit_solution, submit_solution output (последний должен быть в файле,
    # но игнорироваться trace_mapper'ом — это проверится позже).
    assert len(lines) >= 6
    assert lines[0]["payload"]["role"] == "user"
    assert lines[0]["payload"]["content"] == "расскажи об этом проекте"
    # function_call read_file присутствует
    assert any(
        l["payload"].get("type") == "function_call" and l["payload"].get("name") == "read_file"
        for l in lines
    )
