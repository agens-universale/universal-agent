"""stdin_bridge: подмена sys.stdin на queue-backed source, input() читает оттуда."""
import queue
import sys
import threading

from tui.services.stdin_bridge import QueueStdin, redirect_stdin


def test_queue_stdin_readline_blocks_until_item():
    q: "queue.Queue[str]" = queue.Queue()
    q.put("hello\n")
    qsi = QueueStdin(q)
    assert qsi.readline() == "hello\n"


def test_redirect_stdin_replaces_during_block():
    q: "queue.Queue[str]" = queue.Queue()
    original = sys.stdin

    def _runner_thread(result: list) -> None:
        with redirect_stdin(q):
            # input() читает с sys.stdin.readline()
            result.append(input())

    result: list = []
    t = threading.Thread(target=_runner_thread, args=(result,))
    t.start()
    q.put("answer\n")
    t.join(timeout=2.0)

    assert result == ["answer"]
    assert sys.stdin is original  # восстановлен после context
