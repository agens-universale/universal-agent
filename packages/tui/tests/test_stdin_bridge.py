"""stdin_bridge: подмена sys.stdin на queue-backed source, input() читает оттуда."""
import queue
import sys
import threading

from gc_tui.services.stdin_bridge import QueueStdin, redirect_stdin


def test_queue_stdin_readline_blocks_until_item():
    q: queue.Queue[str] = queue.Queue()
    q.put("hello\n")
    qsi = QueueStdin(q)
    assert qsi.readline() == "hello\n"


def test_redirect_stdin_replaces_during_block():
    q: queue.Queue[str] = queue.Queue()

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
    # sys.stdin теперь потоко-aware прокси (установлен один раз, обратно не свапаем);
    # у текущего (main) потока источник не назначен.
    from gc_tui.services.stdin_bridge import _ThreadAwareStdin, _tls
    assert isinstance(sys.stdin, _ThreadAwareStdin)
    assert getattr(_tls, "stdin", None) is None


def test_two_threads_read_own_queue():
    """Регрессия бага: два потока, каждый со своей очередью внутри redirect_stdin,
    одновременно — input() в каждом читает СВОЮ очередь (нет затирания глобального
    sys.stdin)."""
    qa: queue.Queue[str] = queue.Queue()
    qb: queue.Queue[str] = queue.Queue()
    results: dict = {}
    inside = threading.Barrier(3)  # оба worker'а + main внутри/у барьера

    def worker(name: str, q: "queue.Queue[str]") -> None:
        with redirect_stdin(q):
            inside.wait()          # дождаться, пока ОБА потока внутри redirect_stdin
            results[name] = input()

    ta = threading.Thread(target=worker, args=("a", qa))
    tb = threading.Thread(target=worker, args=("b", qb))
    ta.start()
    tb.start()
    inside.wait()                  # оба потока уже подменили stdin (наихудший случай гонки)
    qa.put("AAA\n")
    qb.put("BBB\n")
    ta.join(timeout=2.0)
    tb.join(timeout=2.0)

    assert results == {"a": "AAA", "b": "BBB"}   # каждый прочитал свою очередь


def test_nested_redirect_restores_outer_queue():
    """Вложенный redirect_stdin: после выхода из внутреннего input() снова читает
    внешнюю очередь (а не None/original)."""
    outer: queue.Queue[str] = queue.Queue()
    inner: queue.Queue[str] = queue.Queue()
    got: list = []

    def worker() -> None:
        with redirect_stdin(outer):
            with redirect_stdin(inner):
                inner.put("IN\n")
                got.append(input())     # читает inner
            outer.put("OUT\n")
            got.append(input())          # вернулись к outer

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=2.0)

    assert got == ["IN", "OUT"]
