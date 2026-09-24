"""Лестница точек отсчёта: выбор базы «Сессии» для одного файла (§3.2 спеки волны 4).

У файла лестница состояний: начало сессии → принято → коммит → диск. База
«Сессии» — ближайшая ступень вниз от текущего диска:

1. Ступень HEAD существует ⇔ ``rel`` задан ∧ ``git`` задан ∧
   ``session_snapshot is not None`` ∧ ``committed_since_snapshot`` ∧
   ``not git.has_filter(rel)`` ∧ ``git.head_bytes(rel) is not None`` ∧
   ``head_bytes != session_snapshot``.
2. Есть только ``.accepted`` → она; только HEAD → она; **обе** → HEAD, если
   ``git.head_oid(rel) != accepted.head_oid`` или ``accepted.head_oid is None``
   (коммит после принятия / неразрешимо → безопасная сторона Р28); иначе
   ``.accepted``.
3. Ни одной → ``None`` (вызывающий работает по правилам волны 3).

У пути, СОЗДАННОГО в этой сессии, снимка нет и не будет — вызывающий подаёт
``session_snapshot=b""`` (база созданного — пустота, §3.1) и признак коммита по
отметке создания (``SnapshotStore.created_head_rev``): ``b""`` — законный
снимок, гвард правила 1 отсекает только ``None``.

``committed_since_snapshot`` — ЗАПИСАННЫЙ факт (ревизия HEAD в момент съёма
``.orig``, см. ``SnapshotStore.capture_session``), а не вывод из содержимого.
Сравнение «HEAD отличается от снимка начала сессии» даёт то же неравенство у
файла, который был ГРЯЗЕН ДО сессии: снимок снят с диска и несёт
несохранённые правки пользователя, HEAD их не знает, а коммита не было вовсе.
Ступень HEAD в этом случае — ложная, и откат записал бы содержимое коммита
поверх работы, которой нет ни в индексе, ни в истории. Признака нет
(``False``) → ступени HEAD нет: лишние строки на экране дешевле потерянных
данных.

Слой данных: без Textual, без UI-строк, без git-вызовов и файловых операций —
git-факты инжектируются протоколом ``GitFacts``, тесты обходятся без
репозитория.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gc_core.agent_edits.snapshots import AcceptedState

STEP_ACCEPTED = "accepted"
STEP_HEAD = "head"


@dataclass(frozen=True)
class SessionStep:
    """Ступень лестницы, выбранная как база «Сессии».

    UI-строк здесь нет (Global Constraints: ``agent_edits`` без UI-строк) —
    подписи целей по ``ts``/``commit_ref`` собирает сервис TUI.
    """

    kind: str                # STEP_ACCEPTED | STEP_HEAD
    content: bytes | None    # None только при kind=STEP_ACCEPTED и принятом «отсутствии»
    ts: float | None         # момент принятия (kind=STEP_ACCEPTED), иначе None
    commit_ref: str | None   # сырой ref коммита (kind=STEP_HEAD), иначе None


class GitFacts(Protocol):
    """Git-факты, нужные лестнице. Реализует сервис (с кэшем); в тестах — стаб."""

    def head_bytes(self, rel: str) -> bytes | None: ...      # head_bytes_like_disk(HEAD)

    def head_oid(self, rel: str) -> str | None: ...          # из head_blob_oids

    def has_filter(self, rel: str) -> bool: ...

    def head_ref(self, rel: str) -> str | None: ...          # сырой ref последнего коммита пути


def session_step(
    path_key: str,
    rel: str | None,
    accepted: AcceptedState | None,
    session_snapshot: bytes | None,
    git: GitFacts | None,
    *,
    committed_since_snapshot: bool,
) -> SessionStep | None:
    """База «Сессии» для файла — ближайшая ступень лестницы (правила см. в докстринге модуля).

    ``committed_since_snapshot`` — обязательный именованный аргумент: у
    признака ступени HEAD нет безопасного «наверное» (см. докстринг модуля),
    и вызывающий обязан сказать про него явно.
    """
    head: SessionStep | None = None
    if (
        rel is not None
        and git is not None
        and session_snapshot is not None
        and committed_since_snapshot
        and not git.has_filter(rel)
    ):
        head_bytes = git.head_bytes(rel)
        if head_bytes is not None and head_bytes != session_snapshot:
            head = SessionStep(
                kind=STEP_HEAD, content=head_bytes, ts=None, commit_ref=git.head_ref(rel)
            )

    if accepted is None:
        return head

    accepted_step = SessionStep(
        kind=STEP_ACCEPTED, content=accepted.content, ts=accepted.ts, commit_ref=None
    )
    if head is None:
        return accepted_step
    if accepted.head_oid is None or git.head_oid(rel) != accepted.head_oid:
        return head
    return accepted_step
