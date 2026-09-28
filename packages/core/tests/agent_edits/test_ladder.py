"""session_step: выбор ступени лестницы отсчёта — чистые правила, без git."""

from __future__ import annotations

from gc_core.agent_edits.ladder import (
    STEP_ACCEPTED,
    STEP_HEAD,
    SessionStep,
    session_step,
)
from gc_core.agent_edits.snapshots import AcceptedState


class _Git:
    def __init__(
        self,
        *,
        head: bytes | None = None,
        oid: str | None = None,
        filtered: bool = False,
        ref: str | None = None,
        absent: bool = False,
    ) -> None:
        self._head = head
        self._oid = oid
        self._filtered = filtered
        self._ref = ref
        self._absent = absent

    def head_bytes(self, rel: str) -> bytes | None:
        return self._head

    def head_oid(self, rel: str) -> str | None:
        return self._oid

    def has_filter(self, rel: str) -> bool:
        return self._filtered

    def head_ref(self, rel: str) -> str | None:
        return self._ref

    def head_absent(self, rel: str) -> bool:
        return self._absent


def test_no_steps_returns_none():
    assert (
        session_step(None, None, None, None, committed_since_snapshot=False)
        is None
    )


def test_accepted_only():
    acc = AcceptedState(content=b"acc", ts=5.0, head_oid="oid1")
    step = session_step(None, acc, None, None, committed_since_snapshot=False)
    assert step == SessionStep(
        kind=STEP_ACCEPTED, content=b"acc", ts=5.0, commit_ref=None
    )


def test_head_only_requires_snapshot_and_no_filter():
    # без session_snapshot → None; с фильтром → None; head==snapshot → None
    git = _Git(head=b"new", oid="oid1")
    assert (
        session_step("a.py", None, None, git, committed_since_snapshot=True)
        is None
    )

    filtered_git = _Git(head=b"new", oid="oid1", filtered=True)
    assert (
        session_step(
            "a.py",
            None,
            b"orig",
            filtered_git,
            committed_since_snapshot=True,
        )
        is None
    )

    unchanged_git = _Git(head=b"orig", oid="oid1")
    assert (
        session_step(
            "a.py",
            None,
            b"orig",
            unchanged_git,
            committed_since_snapshot=True,
        )
        is None
    )


def test_rel_none_with_valid_git_returns_none():
    # Файл вне launch_root теперь получает не None, а абсолютный ключ
    # (canonical_path_key, задача 3) — rel=None сюда подаёт сам тест, а git
    # для того же git-проекта остаётся валидным (rel и git не коррелируют, в
    # отличие от прочих тестов файла) — регрессия на гварде `rel is not None`
    # иначе прошла бы незамеченной
    git = _Git(head=b"new", oid="oid1")
    assert (
        session_step(None, None, b"orig", git, committed_since_snapshot=True)
        is None
    )


def test_head_bytes_none_returns_none():
    # путь отсутствует в HEAD (head_bytes=None) — это «ступени HEAD нет», а НЕ
    # SessionStep(kind=STEP_HEAD, content=None): None != session_snapshot тоже
    # True, поэтому гвард `head_bytes is not None` обязателен отдельно
    git = _Git(head=None, oid="oid1")
    assert (
        session_step("a.py", None, b"orig", git, committed_since_snapshot=True)
        is None
    )


def test_head_only_wins_when_no_accepted():
    git = _Git(head=b"new", oid="oid1", ref="e5f2c1a")
    step = session_step(
        "a.py", None, b"orig", git, committed_since_snapshot=True
    )
    assert step == SessionStep(
        kind=STEP_HEAD, content=b"new", ts=None, commit_ref="e5f2c1a"
    )


def test_both_head_younger_by_oid():
    acc = AcceptedState(content=b"acc", ts=1.0, head_oid="old")
    step = session_step(
        "a.py",
        acc,
        b"orig",
        _Git(head=b"new", oid="new"),
        committed_since_snapshot=True,
    )
    assert step.kind == STEP_HEAD


def test_both_accepted_wins_when_oid_matches():
    acc = AcceptedState(content=b"acc", ts=1.0, head_oid="same")
    step = session_step(
        "a.py",
        acc,
        b"orig",
        _Git(head=b"new", oid="same"),
        committed_since_snapshot=True,
    )
    assert step == SessionStep(
        kind=STEP_ACCEPTED, content=b"acc", ts=1.0, commit_ref=None
    )


def test_both_unresolvable_oid_falls_to_head():  # accepted.head_oid=None → HEAD
    acc = AcceptedState(content=b"acc", ts=1.0, head_oid=None)
    git = _Git(head=b"new", oid="new", ref="deadbee")
    step = session_step(
        "a.py", acc, b"orig", git, committed_since_snapshot=True
    )
    assert step == SessionStep(
        kind=STEP_HEAD, content=b"new", ts=None, commit_ref="deadbee"
    )


def test_accepted_absent_marker_flows_through():
    # принято «отсутствие» → SessionStep(content=None)
    acc = AcceptedState(content=None, ts=2.0, head_oid=None)
    step = session_step(None, acc, None, None, committed_since_snapshot=False)
    assert step == SessionStep(
        kind=STEP_ACCEPTED, content=None, ts=2.0, commit_ref=None
    )


def test_head_step_requires_recorded_commit_flag_not_content_difference():
    """C1: HEAD ≠ снимок начала сессии — НЕ признак коммита.

    Ровно то же неравенство даёт файл, который был грязен ДО сессии: снимок
    снят с диска и несёт несохранённые правки пользователя, HEAD их не знает,
    коммита не было. Без записанного признака ступени HEAD нет — откат
    вернётся к `.orig`, а не запишет содержимое коммита поверх работы
    пользователя.
    """
    git = _Git(head=b"committed", oid="oid1", ref="e5f2c1a")

    assert (
        session_step(
            "a.py",
            None,
            b"dirty-before-session",
            git,
            committed_since_snapshot=False,
        )
        is None
    )

    step = session_step(
        "a.py",
        None,
        b"dirty-before-session",
        git,
        committed_since_snapshot=True,
    )
    assert step == SessionStep(
        kind=STEP_HEAD,
        content=b"committed",
        ts=None,
        commit_ref="e5f2c1a",
    )


def test_empty_snapshot_of_a_created_file_still_gives_a_head_step():
    """Д1: у файла, созданного здесь, снимок начала сессии — ПУСТОТА (§3.1).

    ``b""`` — законный снимок, а не «снимка нет»: гвард правила 1 отсекает
    только ``None``. Подмена гварда на «истинность» отняла бы ступень HEAD у
    каждого созданного и закоммиченного файла.
    """
    git = _Git(head=b"committed", oid="oid1", ref="e5f2c1a")

    step = session_step(
        "new.py", None, b"", git, committed_since_snapshot=True
    )

    assert step == SessionStep(
        kind=STEP_HEAD,
        content=b"committed",
        ts=None,
        commit_ref="e5f2c1a",
    )


def test_accepted_survives_without_commit_flag():
    """Принятая ступень признака коммита не спрашивает: без него она и остаётся
    базой (иначе фикс C1 забрал бы у пользователя ещё и `.accepted`)."""
    acc = AcceptedState(content=b"acc", ts=5.0, head_oid=None)
    git = _Git(head=b"committed", oid="oid1", ref="e5f2c1a")

    step = session_step(
        "a.py", acc, b"orig", git, committed_since_snapshot=False
    )

    assert step == SessionStep(
        kind=STEP_ACCEPTED, content=b"acc", ts=5.0, commit_ref=None
    )


def test_a_committed_deletion_is_a_head_step_of_absence():
    """Удаление, закоммиченное после снимка, — ступень HEAD «файла нет».

    Без неё откат сессии возвращал старую копию файла, удаление которого уже
    в коммите, — то есть уходил ниже коммита (ревью PR, второй коллега,
    находка 1). Отсутствие должно быть ДОКАЗАНО (``head_absent``): голое
    «байтов нет» бывает и отказом git, и ступень «удалить» по нему удалила бы
    файл пользователя."""
    git = _Git(head=None, absent=True, ref="d1e2l3")
    step = session_step(
        "a.py", None, b"orig", git, committed_since_snapshot=True
    )
    assert step == SessionStep(
        kind=STEP_HEAD, content=None, ts=None, commit_ref="d1e2l3"
    )


def test_accepted_after_the_committed_deletion_wins():
    """Принятие при уже удалённом в HEAD пути (oid принятия пуст) — моложе коммита."""
    acc = AcceptedState(content=b"acc", ts=5.0, head_oid=None)
    step = session_step(
        "a.py",
        acc,
        b"orig",
        _Git(head=None, absent=True),
        committed_since_snapshot=True,
    )
    assert step.kind == STEP_ACCEPTED


def test_deletion_committed_after_the_accept_wins():
    """Принятие при пути, ещё бывшем в HEAD, — коммит удаления моложе принятия."""
    acc = AcceptedState(content=b"acc", ts=5.0, head_oid="was-there")
    step = session_step(
        "a.py",
        acc,
        b"orig",
        _Git(head=None, absent=True),
        committed_since_snapshot=True,
    )
    assert step.kind == STEP_HEAD and step.content is None
