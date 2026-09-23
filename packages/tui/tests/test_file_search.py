"""Сеанс фонового поиска пикера: один поток-демон, последний запрос побеждает."""

import logging
import threading
import time

from gc_tui.services.file_search import FileSearchSession


class _FakeIndex:
    """Подмена PathIndex: сборку и поиск можно придержать событием."""

    def __init__(self, *, truncated=False):
        self.root = "proj-root"
        self.truncated = truncated
        self.build_gate = threading.Event()
        self.build_gate.set()
        self.build_entered = threading.Event()
        self.search_gate = threading.Event()
        self.search_gate.set()
        self.search_entered = threading.Event()
        self.builds = 0
        self.searched = []

    def build(self):
        self.builds += 1
        self.build_entered.set()
        self.build_gate.wait(timeout=5)

    def search(self, query):
        self.searched.append(query)
        self.search_entered.set()
        self.search_gate.wait(timeout=5)
        return [query + ".py"]


def _session(index):
    """Сеанс, его поток (единственный новый) и список отданных результатов."""
    results = []
    before = set(threading.enumerate())
    session = FileSearchSession(index, lambda query, entries, truncated: results.append((query, entries, truncated)))
    (thread,) = set(threading.enumerate()) - before
    return session, thread, results


def test_result_arrives_from_search_thread():
    index = _FakeIndex(truncated=True)
    seen = []
    session = FileSearchSession(
        index,
        lambda query, entries, truncated: seen.append(
            (query, entries, truncated, threading.current_thread() is threading.main_thread())
        ),
    )
    session.want("abc")
    assert session.wait_idle(5)
    assert seen == [("abc", ["abc.py"], True, False)]
    session.close()


def test_latest_query_wins_while_index_builds():
    index = _FakeIndex()
    index.build_gate.clear()
    session, _thread, results = _session(index)
    session.want("a")
    assert index.build_entered.wait(5)
    session.want("ab")
    session.want("abc")
    index.build_gate.set()
    assert session.wait_idle(5)
    assert index.searched == ["abc"]
    assert results == [("abc", ["abc.py"], False)]
    session.close()


def test_query_changed_during_search_is_searched_next():
    index = _FakeIndex()
    index.search_gate.clear()
    session, _thread, results = _session(index)
    session.want("q1")
    assert index.search_entered.wait(5)
    session.want("q2")
    index.search_gate.set()
    assert session.wait_idle(5)
    assert results == [("q1", ["q1.py"], False), ("q2", ["q2.py"], False)]
    session.close()


def test_same_query_again_is_searched_again():
    """Набрали «ab», дописали «c», стёрли до «ab»: повтор считается заново —
    пропуск «равен последнему посчитанному» оставил бы Enter ждать вечно."""
    index = _FakeIndex()
    index.search_gate.clear()
    session, _thread, results = _session(index)
    session.want("ab")
    assert index.search_entered.wait(5)
    session.want("abc")
    session.want("ab")
    index.search_gate.set()
    assert session.wait_idle(5)
    assert [query for query, _entries, _truncated in results] == ["ab", "ab"]
    session.close()


def test_close_during_build_skips_search_and_ends_thread():
    index = _FakeIndex()
    index.build_gate.clear()
    session, thread, results = _session(index)
    session.want("abc")
    assert index.build_entered.wait(5)
    started = time.perf_counter()
    session.close()
    assert time.perf_counter() - started < 1.0  # close не ждёт поток (сборка ещё придержана)
    index.build_gate.set()
    thread.join(5)
    assert not thread.is_alive()
    assert index.searched == []
    assert results == []


def test_close_during_search_drops_its_result():
    index = _FakeIndex()
    index.search_gate.clear()
    session, thread, results = _session(index)
    session.want("abc")
    assert index.search_entered.wait(5)
    session.close()
    index.search_gate.set()
    thread.join(5)
    assert not thread.is_alive()
    assert results == []


def test_close_without_queries_ends_thread():
    session, thread, _results = _session(_FakeIndex())
    session.close()
    thread.join(5)
    assert not thread.is_alive()


def test_search_failure_gives_empty_result_and_thread_survives(caplog):
    index = _FakeIndex()
    real_search = index.search

    def search(query):
        if query == "bad":
            raise RuntimeError("index exploded")
        return real_search(query)

    index.search = search
    session, _thread, results = _session(index)
    with caplog.at_level(logging.WARNING, logger="gc_tui.services.file_search"):
        session.want("bad")
        assert session.wait_idle(5)
    session.want("ok")
    assert session.wait_idle(5)
    assert results == [("bad", [], False), ("ok", ["ok.py"], False)]
    assert any(
        "file search failed" in r.getMessage() and "proj-root" in r.getMessage() and "'bad'" in r.getMessage()
        for r in caplog.records
    )
    session.close()


def test_build_failure_gives_empty_result_once():
    index = _FakeIndex()

    def build():
        index.builds += 1
        raise OSError("disk gone")

    index.build = build
    session, _thread, results = _session(index)
    session.want("abc")
    assert session.wait_idle(5)
    assert results == [("abc", [], False)]
    assert index.builds == 1
    session.close()


def test_one_daemon_thread_serves_all_queries():
    before = set(threading.enumerate())
    session = FileSearchSession(_FakeIndex(), lambda query, entries, truncated: None)
    for query in ("a", "ab", "abc", "abcd"):
        session.want(query)
    assert session.wait_idle(5)
    new = set(threading.enumerate()) - before
    assert [(t.name, t.daemon) for t in new] == [("file-search", True)]
    session.close()
