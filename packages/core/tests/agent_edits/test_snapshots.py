"""SnapshotStore: per-thread снапшоты .orig/.turn с turn-индексом и LRU."""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest

from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path


def _store(tmp_path: Path, thread_key: str = "sys_th1") -> SnapshotStore:
    return SnapshotStore(tmp_path / "diffs", thread_key)


def _accepted_file_of(store: SnapshotStore, path_key: str) -> Path:
    """Путь байтового снимка `.accepted` для ``path_key`` — по наименованию
    стора (``<sha256(path_key)>_<thread_key>``, см. докстринг модуля)."""
    stem = f"{hashlib.sha256(path_key.encode('utf-8')).hexdigest()}_{store.thread_key}"
    return store.root / f"{stem}.accepted"


def test_capture_session_writes_once_repeated_call_is_noop(tmp_path):
    store = _store(tmp_path)
    store.capture_session("a.py", b"first content")
    store.capture_session("a.py", b"second content, must be ignored")

    assert store.session_baseline("a.py") == b"first content"


def test_snapshots_root_gets_internal_gitignore(tmp_path):
    """Служебный каталог не должен светиться в git status пользовательского проекта."""
    store = SnapshotStore(snapshots_root(str(tmp_path)), "sys_th")
    store.capture_session("a.py", b"x")

    ignore = snapshots_root(str(tmp_path)) / ".gitignore"
    assert ignore.is_file() and ignore.read_text(encoding="utf-8").strip() == "*"


def test_existing_gitignore_is_not_overwritten(tmp_path):
    root = snapshots_root(str(tmp_path))
    root.mkdir(parents=True)
    (root / ".gitignore").write_text("# своё\n", encoding="utf-8")

    SnapshotStore(root, "sys_th").capture_session("a.py", b"x")

    assert (root / ".gitignore").read_text(encoding="utf-8") == "# своё\n"


def test_capture_session_reports_whether_it_wrote(tmp_path):
    store = SnapshotStore(snapshots_root(str(tmp_path)), "sys_th")
    assert store.capture_session("a.py", b"first") is True
    assert store.capture_session("a.py", b"second") is False      # baseline уже есть
    assert store.session_baseline("a.py") == b"first"


def test_capture_turn_of_same_turn_is_not_clobbered(tmp_path):
    store = SnapshotStore(snapshots_root(str(tmp_path)), "sys_th")
    store.capture_turn("a.py", b"base", "t1")
    store.capture_turn("a.py", b"later", "t1")     # тот же ход — ранний выход
    assert store.turn_baseline("a.py", "t1") == b"base"


def test_capture_turn_of_new_turn_replaces_previous(tmp_path):
    store = SnapshotStore(snapshots_root(str(tmp_path)), "sys_th")
    store.capture_turn("a.py", b"t1base", "t1")
    store.capture_turn("a.py", b"t2base", "t2")    # ход сменился — снимок перевыставлен
    assert store.turn_baseline("a.py", "t2") == b"t2base"
    assert store.turn_baseline("a.py", "t1") is None   # протухший снимок не отдаётся


def test_capture_turn_overwrites_on_new_turn_keeps_on_same_turn(tmp_path):
    store = _store(tmp_path)
    store.capture_turn("a.py", b"turn1-v1", "turn-1")
    store.capture_turn("a.py", b"turn1-v2-same-turn-ignored", "turn-1")
    assert store.turn_baseline("a.py", "turn-1") == b"turn1-v1"

    store.capture_turn("a.py", b"turn2-content", "turn-2")
    assert store.turn_baseline("a.py", "turn-2") == b"turn2-content"


def test_baselines_roundtrip_non_utf8_bytes_exactly(tmp_path):
    store = _store(tmp_path)
    raw = b"\xff\xfe\x00binary"
    store.capture_session("bin.dat", raw)
    store.capture_turn("bin.dat", raw, "turn-1")

    assert store.session_baseline("bin.dat") == raw
    assert store.turn_baseline("bin.dat", "turn-1") == raw


def test_turn_baseline_returns_none_for_foreign_turn_id(tmp_path):
    store = _store(tmp_path)
    store.capture_turn("a.py", b"content", "turn-1")

    assert store.turn_baseline("a.py", "turn-OTHER") is None


def test_capture_shell_session_writes_once_and_reports_it(tmp_path):
    store = _store(tmp_path)

    assert store.capture_shell_session("a.py", b"index") is True
    assert store.capture_shell_session("a.py", b"index-2") is False
    assert store.counting_session_baseline("a.py") == b"index"


def test_counting_session_baseline_takes_the_earlier_of_two_bases(tmp_path):
    """Для счёта — более ранняя из двух баз, даже если точная МОЛОЖЕ счётной.

    Поверх счётной базы точную API больше не пишет
    (`test_exact_base_is_not_taken_over_a_counting_one`), но хранилища,
    записанные до этого правила, такую пару содержат: шелл положил `.gitorig`
    (истинное «до»), следом рекордер — `.orig` уже ПОСЛЕ команды. Для счёта
    там нужна ранняя база, иначе дельта команды молча выпадает из кумулятива;
    поэтому поздний `.orig` здесь кладётся напрямую, как наследие.
    """
    store = _store(tmp_path)
    store.capture_shell_session("a.py", b"true-before\n")
    store._session_path("a.py").write_bytes(b"after-command\n")

    root = tmp_path / "diffs"
    now = time.time()
    os.utime(next(root.glob("*.gitorig")), (now - 30, now - 30))
    os.utime(next(root.glob("*.orig")), (now - 10, now - 10))

    assert store.counting_session_baseline("a.py") == b"true-before\n"
    assert store.session_baseline("a.py") == b"after-command\n"


def test_counting_session_baseline_takes_precise_when_it_is_earlier(tmp_path):
    """Зеркало предыдущего кейса: правило — «более ранняя», а не «всегда счётная».

    Обычный порядок (сначала файл правил fs-тул, потом та же правка догнала
    команду) обязан дать точную базу — подмена правила на «всегда .gitorig»
    здесь краснеет.
    """
    store = _store(tmp_path)
    store.capture_session("a.py", b"true-before\n")
    store.capture_shell_session("a.py", b"after-tool\n")

    root = tmp_path / "diffs"
    now = time.time()
    os.utime(next(root.glob("*.orig")), (now - 30, now - 30))
    os.utime(next(root.glob("*.gitorig")), (now - 10, now - 10))

    assert store.counting_session_baseline("a.py") == b"true-before\n"


def test_counting_turn_baseline_takes_precise_when_it_is_earlier(tmp_path):
    store = _store(tmp_path)
    store.capture_turn("a.py", b"true-before\n", "turn-1")
    store.capture_shell_turn("a.py", b"after-tool\n", "turn-1")

    root = tmp_path / "diffs"
    now = time.time()
    os.utime(next(root.glob("*.turn")), (now - 30, now - 30))
    os.utime(next(root.glob("*.gitturn")), (now - 10, now - 10))

    assert store.counting_turn_baseline("a.py", "turn-1") == b"true-before\n"


def test_counting_session_baseline_takes_the_only_base_available(tmp_path):
    store = _store(tmp_path)
    store.capture_session("only-precise.py", b"precise")
    store.capture_shell_session("only-counting.py", b"counting")

    assert store.counting_session_baseline("only-precise.py") == b"precise"
    assert store.counting_session_baseline("only-counting.py") == b"counting"
    assert store.counting_session_baseline("nothing.py") is None


def test_counting_turn_baseline_takes_the_earlier_of_two_bases(tmp_path):
    """То же для хода: поздний `.turn` поверх `.gitturn` — наследие старых хранилищ."""
    store = _store(tmp_path)
    store.capture_shell_turn("a.py", b"true-before\n", "turn-1")
    store._turn_path("a.py").write_bytes(b"after-command\n")
    store._mark("a.py", {"turn_id": "turn-1"})

    root = tmp_path / "diffs"
    now = time.time()
    os.utime(next(root.glob("*.gitturn")), (now - 30, now - 30))
    os.utime(next(root.glob("*.turn")), (now - 10, now - 10))

    assert store.counting_turn_baseline("a.py", "turn-1") == b"true-before\n"
    assert store.turn_baseline("a.py", "turn-1") == b"after-command\n"


def test_counting_turn_baseline_ignores_stale_shell_turn(tmp_path):
    """Протухшая база чужого хода выглядела бы точной — Р12 нарушился бы молча."""
    store = _store(tmp_path)
    store.capture_shell_turn("a.py", b"turn1", "turn-1")

    assert store.counting_turn_baseline("a.py", "turn-1") == b"turn1"
    assert store.counting_turn_baseline("a.py", "turn-OTHER") is None

    store.capture_shell_turn("a.py", b"turn2", "turn-2")
    assert store.counting_turn_baseline("a.py", "turn-2") == b"turn2"
    assert store.counting_turn_baseline("a.py", "turn-1") is None


def test_counting_and_precise_turn_indexes_coexist(tmp_path):
    """Две породы делят одну запись индекса и не затирают отметку друг друга."""
    store = _store(tmp_path)
    store.capture_turn("a.py", b"precise", "turn-1")
    store.capture_shell_turn("a.py", b"counting", "turn-1")

    assert store.turn_baseline("a.py", "turn-1") == b"precise"
    assert store.has_turn_baseline("a.py", "turn-1") is True
    assert store.counting_turn_baseline("a.py", "turn-1") is not None


def test_has_baseline_predicates_see_only_precise_snapshots(tmp_path):
    """Откату годится только точный снимок: счётная база из индекса git не
    содержит несохранённых ручных правок пользователя."""
    store = _store(tmp_path)
    store.capture_shell_session("a.py", b"index")
    store.capture_shell_turn("a.py", b"index", "turn-1")

    assert store.has_session_baseline("a.py") is False
    assert store.has_turn_baseline("a.py", "turn-1") is False
    assert store.session_baseline("a.py") is None
    assert store.turn_baseline("a.py", "turn-1") is None

    # Точная — у другого пути: поверх счётной базы того же она не ложится.
    store.capture_session("b.py", b"precise")
    store.capture_turn("b.py", b"precise", "turn-1")

    assert store.has_session_baseline("b.py") is True
    assert store.has_turn_baseline("b.py", "turn-1") is True


def test_evict_lru_counts_and_removes_counting_bases(tmp_path):
    """Счётная база живёт в том же каталоге и обязана считаться в лимите."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)  # пусто — тред без журнала, снимки evictable

    store = SnapshotStore(diffs_root, "th-a", threads_dir)
    store.capture_shell_session("a.py", b"a")
    store.capture_shell_turn("a.py", b"a", "turn-1")
    store.capture_session("b.py", b"b")

    now = time.time()
    os.utime(next(diffs_root.glob("*.gitorig")), (now - 30, now - 30))
    os.utime(next(diffs_root.glob("*.gitturn")), (now - 20, now - 20))
    os.utime(next(diffs_root.glob("*.orig")), (now - 10, now - 10))

    store.evict_lru(max_files=1)

    assert store.counting_session_baseline("a.py") is None
    assert store.counting_turn_baseline("a.py", "turn-1") is None
    assert store.session_baseline("b.py") == b"b"
    # отметка вынесенной .gitturn убрана из индекса вместе с файлом
    assert "shell_turn_id" not in (tmp_path / "diffs" / "index.json").read_text(
        encoding="utf-8"
    )


def test_evict_lru_spares_thread_with_real_journal_path(tmp_path):
    """thread_key = f"{system_id}_{thread_id}" — та же строка, что в имени
    журнала (thread_store.edits_journal_path). Точное совпадение имени,
    без wildcard: тред без журнала защиты не получает."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)
    edits_journal_path(str(threads_dir), "sys", "th").write_text("", encoding="utf-8")

    store_alive = SnapshotStore(diffs_root, "sys_th", threads_dir)
    store_dead = SnapshotStore(diffs_root, "sys_other", threads_dir)
    store_alive.capture_session("a.py", b"alive content")
    store_dead.capture_session("b.py", b"dead content")

    # 2 файла, лимит 1: единственный кандидат — dead (без журнала)
    store_alive.evict_lru(max_files=1)

    assert store_alive.session_baseline("a.py") == b"alive content"
    assert store_dead.session_baseline("b.py") is None


def test_evict_lru_evicts_legacy_before_older_evictable_snapshots(tmp_path):
    """Legacy — кандидат №1 независимо от mtime: даже самый свежий legacy-файл
    уходит раньше более старых, но не-legacy evictable-снапшотов (наивная
    сортировка «всё по mtime» снесла бы th-a, а не legacy)."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)  # пусто — оба треда без журнала (evictable)

    store_a = SnapshotStore(diffs_root, "th-a", threads_dir)
    store_b = SnapshotStore(diffs_root, "th-b", threads_dir)
    store_a.capture_session("a.py", b"a")
    store_b.capture_session("b.py", b"b")

    legacy = diffs_root / (("0" * 64) + ".orig")
    legacy.write_bytes(b"legacy content")

    now = time.time()
    os.utime(next(diffs_root.glob("*_th-a.orig")), (now - 30, now - 30))
    os.utime(next(diffs_root.glob("*_th-b.orig")), (now - 20, now - 20))
    os.utime(legacy, (now, now))  # legacy — самый новый по mtime

    # 3 файла, лимит 2 → overflow=1: должен уйти именно legacy
    store_a.evict_lru(max_files=2)

    assert not legacy.exists()
    assert store_a.session_baseline("a.py") == b"a"
    assert store_b.session_baseline("b.py") == b"b"


def test_evict_lru_removes_oldest_mtime_first_among_evictable(tmp_path):
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)  # пусто — ни у одного треда нет журнала

    store_a = SnapshotStore(diffs_root, "th-a", threads_dir)
    store_b = SnapshotStore(diffs_root, "th-b", threads_dir)
    store_c = SnapshotStore(diffs_root, "th-c", threads_dir)
    store_a.capture_session("a.py", b"a")
    store_b.capture_session("b.py", b"b")
    store_c.capture_session("c.py", b"c")

    now = time.time()
    os.utime(next(diffs_root.glob("*_th-a.orig")), (now - 30, now - 30))
    os.utime(next(diffs_root.glob("*_th-b.orig")), (now - 20, now - 20))
    os.utime(next(diffs_root.glob("*_th-c.orig")), (now - 10, now - 10))

    store_a.evict_lru(max_files=2)

    assert store_a.session_baseline("a.py") is None  # самый старый — вытеснен
    assert store_b.session_baseline("b.py") == b"b"
    assert store_c.session_baseline("c.py") == b"c"


# --- смерть legacy-стора -----------------------------------------------------


def test_legacy_diffs_store_is_dropped_once(tmp_path):
    """Умерший `.gc/diffs` сносится при заведении нового корня снапшотов."""
    legacy = tmp_path / ".gc" / "diffs"
    legacy.mkdir(parents=True)
    (legacy / ("a" * 64 + ".orig")).write_bytes(b"x")
    (legacy / "index.json").write_text("{}", encoding="utf-8")

    SnapshotStore(snapshots_root(str(tmp_path)), "sys_th").capture_session("a.py", b"y")

    assert not legacy.exists()


def test_legacy_store_with_foreign_files_is_kept(tmp_path):
    """Чужие файлы под тем же именем каталога — не наш стор, не трогаем."""
    legacy = tmp_path / ".gc" / "diffs"
    legacy.mkdir(parents=True)
    (legacy / "notes.txt").write_text("важное", encoding="utf-8")

    SnapshotStore(snapshots_root(str(tmp_path)), "sys_th").capture_session("a.py", b"y")

    assert (legacy / "notes.txt").is_file()


def _link_dir(link: Path, target: Path, kind: str) -> None:
    """Ссылка на каталог нужного рода; среда не даёт — тест пропускается."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        if kind == "symlink":
            link.symlink_to(target, target_is_directory=True)
            return
        import _winapi                       # junction — только Windows

        _winapi.CreateJunction(str(target), str(link))
    except (OSError, ImportError, NotImplementedError, AttributeError):
        pytest.skip(f"среда не даёт создать {kind} на каталог")


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_legacy_store_link_is_not_followed(tmp_path, kind):
    """Под ссылкой `diffs` лежит ЧУЖОЙ каталог — фильтр по содержимому смотрит
    сквозь неё и вынес бы его файлы, поэтому ссылка не разбирается вовсе.

    Junction проверяется отдельно от symlink: `Path.is_symlink()` на него
    отдаёт False, и одной этой проверки для Windows не хватило бы.
    """
    target = tmp_path / "real"
    target.mkdir()
    victim = target / ("a" * 64 + ".orig")             # проходит фильтр насквозь
    victim.write_bytes(b"x")
    link = tmp_path / ".gc" / "diffs"
    _link_dir(link, target, kind)

    SnapshotStore(snapshots_root(str(tmp_path)), "sys_th").capture_session("a.py", b"y")

    assert victim.is_file()
    assert link.exists()


def test_legacy_store_is_not_rescanned_after_root_exists(tmp_path, monkeypatch):
    """Чистка одноразовая: обход чужого каталога не платится на каждой правке."""
    from gc_core.agent_edits import snapshots as snap_mod

    store = SnapshotStore(snapshots_root(str(tmp_path)), "sys_th")
    store.capture_session("a.py", b"y")          # корень заведён здесь

    calls = []
    monkeypatch.setattr(
        snap_mod, "_drop_legacy_store", lambda root: calls.append(root) or False
    )
    store.capture_session("b.py", b"z")
    assert calls == []


# --- accepted -----------------------------------------------------------


def test_accepted_roundtrip_and_overwrite(tmp_path):
    store = _store(tmp_path)
    store.capture_accepted("a.py", b"one", head_oid="oid1")
    st = store.accepted_state("a.py")
    assert (st.content, st.head_oid) == (b"one", "oid1") and st.ts > 0

    store.capture_accepted("a.py", b"two", head_oid=None)   # повторное принятие перезаписывает
    assert store.accepted_state("a.py").content == b"two"


def test_accepted_absent_marker_is_explicit(tmp_path):
    store = _store(tmp_path)
    store.capture_accepted("gone.py", None, head_oid=None)
    st = store.accepted_state("gone.py")
    assert st is not None and st.content is None            # принято «отсутствие»


def test_accepted_ts_without_bytes_and_marker_is_not_accepted(tmp_path):
    # контракт §3.1: потерянный снимок не читается как маркер
    store = _store(tmp_path)
    store.capture_accepted("a.py", b"data", head_oid=None)
    _accepted_file_of(store, "a.py").unlink()               # снимок потерян
    assert store.accepted_state("a.py") is None


def test_live_accepted_bytes_win_over_a_stale_absent_marker(tmp_path):
    """I4: байты пишутся ДО индекса. Принятие «с содержимым» поверх прежнего
    маркера отсутствия + отказ записи индекса = живой снимок рядом со старым
    `accepted_absent`. Прочитать это как «принято отсутствие» значило бы
    предложить пользователю удалить живой файл — читаем байты.
    """
    store = _store(tmp_path)
    store.capture_accepted("a.py", None, head_oid=None)           # принято отсутствие
    # байты легли, индекс — нет
    _accepted_file_of(store, "a.py").write_bytes("снова принят\n".encode("utf-8"))

    st = store.accepted_state("a.py")

    assert st is not None and st.content == "снова принят\n".encode("utf-8")


def test_clear_accepted_removes_bytes_and_index_keys(tmp_path):
    store = _store(tmp_path)
    store.capture_accepted("a.py", b"content", head_oid="oid1")

    assert store.clear_accepted("a.py") is True

    assert store.accepted_state("a.py") is None
    assert not _accepted_file_of(store, "a.py").exists()
    index_text = (tmp_path / "diffs" / "index.json").read_text(encoding="utf-8")
    assert "accepted_ts" not in index_text
    assert "accepted_head_oid" not in index_text
    assert store.clear_accepted("a.py") is False   # уже пусто — чистить нечего


def test_evict_lru_takes_accepted_and_cleans_index(tmp_path):
    """Сиротский .accepted мёртвого треда вытесняется, accepted_* ключи не сиротеют."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)  # пусто — тред без журнала, снимки evictable

    store = SnapshotStore(diffs_root, "th-a", threads_dir)
    store.capture_accepted("a.py", b"accepted-content", head_oid="oid1")
    store.capture_session("b.py", b"b")

    now = time.time()
    os.utime(next(diffs_root.glob("*.accepted")), (now - 20, now - 20))
    os.utime(next(diffs_root.glob("*.orig")), (now - 10, now - 10))

    store.evict_lru(max_files=1)

    assert store.accepted_state("a.py") is None
    assert store.session_baseline("b.py") == b"b"
    index_text = (diffs_root / "index.json").read_text(encoding="utf-8")
    assert "accepted_ts" not in index_text
    assert "accepted_head_oid" not in index_text


def test_live_journal_thread_accepted_survives_eviction(tmp_path):
    """thread_key = f"{system_id}_{thread_id}" — та же строка, что в имени
    журнала (thread_store.edits_journal_path). Точное совпадение имени,
    без wildcard: тред без журнала защиты не получает."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)
    edits_journal_path(str(threads_dir), "sys", "th").write_text("", encoding="utf-8")

    store_alive = SnapshotStore(diffs_root, "sys_th", threads_dir)
    store_dead = SnapshotStore(diffs_root, "sys_other", threads_dir)
    store_alive.capture_accepted("a.py", b"alive-accepted", head_oid="oid1")
    store_dead.capture_accepted("b.py", b"dead-accepted", head_oid=None)

    # 2 файла, лимит 1: единственный кандидат — dead (без журнала)
    store_alive.evict_lru(max_files=1)

    assert store_alive.accepted_state("a.py").content == b"alive-accepted"
    assert store_dead.accepted_state("b.py") is None


# --- ревизия HEAD рядом со снимком: признак ступени HEAD (C1) ----------------


def test_session_head_rev_is_written_with_the_snapshot(tmp_path):
    store = _store(tmp_path)
    store.capture_session("a.py", b"content", head_rev="a" * 40)

    assert store.session_head_rev("a.py") == "a" * 40


def test_session_head_rev_absent_when_not_recorded(tmp_path):
    """Снимок старого формата (и любой не-git) — отметки нет: ступени HEAD не будет."""
    store = _store(tmp_path)
    store.capture_session("a.py", b"content")

    assert store.session_head_rev("a.py") is None


def test_session_head_rev_is_not_touched_by_noop_capture(tmp_path):
    """Повторный съём — no-op: чужая (уже записанная) ревизия остаётся на месте."""
    store = _store(tmp_path)
    store.capture_session("a.py", b"first", head_rev="a" * 40)
    store.capture_session("a.py", b"second", head_rev="b" * 40)

    assert store.session_head_rev("a.py") == "a" * 40


def test_new_snapshot_without_rev_clears_stale_rev(tmp_path):
    """Снимок вытеснили и сняли заново уже без ревизии — старая отметка обязана
    уйти: чужая ревизия читалась бы как «коммит был» на новом снимке."""
    store = _store(tmp_path)
    store.capture_session("a.py", b"first", head_rev="a" * 40)
    next(store.root.glob("*.orig")).unlink()

    store.capture_session("a.py", b"second")

    assert store.session_head_rev("a.py") is None


def test_session_head_rev_shares_entry_with_turn_marks(tmp_path):
    """Отметки трёх пород делят одну запись индекса — записать одну значит
    не потерять другие."""
    store = _store(tmp_path)
    store.capture_turn("a.py", b"turn", "turn-1")
    store.capture_session("a.py", b"session", head_rev="c" * 40)

    assert store.session_head_rev("a.py") == "c" * 40
    assert store.turn_baseline("a.py", "turn-1") == b"turn"


def test_mark_created_records_the_rev_and_none_means_no_mark(tmp_path):
    """Отметка создания живёт без файла байтов: у созданного файла снимка нет."""
    store = _store(tmp_path)
    store.mark_created("new.py", head_rev="a" * 40)
    store.mark_created("outside-git.py", head_rev=None)

    assert store.created_head_rev("new.py") == "a" * 40
    assert store.created_head_rev("outside-git.py") is None
    assert store.session_baseline("new.py") is None      # байтов у отметки нет


def test_mark_created_survives_a_later_session_snapshot(tmp_path):
    """R2: вторая правка созданного файла пишет `.orig` с пост-созданным
    содержимым и уносит с собой СВОЮ ревизию — отметка создания обязана это
    пережить, иначе ревизия ступени HEAD станет пост-коммитной."""
    store = _store(tmp_path)
    store.mark_created("new.py", head_rev="a" * 40)

    store.capture_session("new.py", b"content after create\n", head_rev="b" * 40)

    assert store.created_head_rev("new.py") == "a" * 40
    assert store.session_head_rev("new.py") == "b" * 40


def test_mark_created_yields_to_a_snapshot_taken_earlier(tmp_path):
    """Снимок снят РАНЬШЕ — путь существовал, и «создан здесь» было бы ложью.

    Обратный порядок к R2: там отметка стоит первой (у созданного пути `.orig`
    появляется только со второй правки), здесь первым лёг снимок. Так приходит
    щель `append_to_file` — существующему файлу нулевого размера тул ставит
    `op="create"`, и путь, усечённый шелл-командой раньше в этой же сессии,
    потерял бы своё честное «до»: лестница считала бы от пустоты.
    """
    store = _store(tmp_path)
    store.capture_session("a.py", b"user work\n", head_rev="a" * 40)

    store.mark_created("a.py", head_rev="b" * 40)

    assert store.created_head_rev("a.py") is None
    assert store.session_baseline("a.py") == b"user work\n"
    assert store.session_head_rev("a.py") == "a" * 40


def test_mark_created_is_evicted_with_thread(tmp_path):
    """Отметка умирает вместе с записями своего треда: файла байтов за ней нет,
    и файловая эвикция сама её не увидела бы — жила бы в индексе вечно."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)
    edits_journal_path(str(threads_dir), "sys", "th").write_text("", encoding="utf-8")

    store_alive = SnapshotStore(diffs_root, "sys_th", threads_dir)
    store_dead = SnapshotStore(diffs_root, "sys_other", threads_dir)
    store_alive.mark_created("a.py", head_rev="a" * 40)
    store_dead.mark_created("b.py", head_rev="b" * 40)

    store_alive.evict_lru(max_files=1)

    assert store_alive.created_head_rev("a.py") == "a" * 40
    assert store_dead.created_head_rev("b.py") is None


def test_evict_lru_keeps_index_entry_of_a_surviving_snapshot(tmp_path):
    """Уборка записей мёртвого треда не трогает те, за которыми остался файл:
    иначе снимок, переживший эвикцию, потерял бы свою turn-отметку."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)  # пусто — тред без журнала, но снимок цел

    store_dead = SnapshotStore(diffs_root, "sys_other", threads_dir)
    store_dead.capture_turn("a.py", b"turn base", "turn-1")

    store_dead.evict_lru(max_files=10)   # переполнения нет — вытеснять нечего

    assert store_dead.turn_baseline("a.py", "turn-1") == b"turn base"


def test_evict_lru_cleans_session_head_rev(tmp_path):
    """Вынесли `.orig` — ревизия не сиротеет в индексе."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)  # пусто — тред без журнала, снимки evictable

    store = SnapshotStore(diffs_root, "th-a", threads_dir)
    store.capture_session("a.py", b"a", head_rev="a" * 40)
    store.capture_accepted("b.py", b"b", head_oid=None)

    now = time.time()
    os.utime(next(diffs_root.glob("*.orig")), (now - 20, now - 20))
    os.utime(next(diffs_root.glob("*.accepted")), (now - 10, now - 10))

    store.evict_lru(max_files=1)

    assert store.session_baseline("a.py") is None
    assert store.session_head_rev("a.py") is None
    assert "session_head_rev" not in (diffs_root / "index.json").read_text(
        encoding="utf-8"
    )


def test_second_writer_does_not_steal_the_first_writers_tmp(tmp_path, monkeypatch):
    """Два писателя одного пути в одном треде не делят временный файл.

    Открытый TUI и `gc-cli` по тому же треду дают одинаковый ключ треда, а
    значит и одинаковый стем снимка. С общим именем временного файла второй
    писатель уносит его из-под первого: либо снимок перемешан байтами (откат
    затрёт файл мусором), либо `os.replace` падает с ENOENT.

    Второй писатель вклинивается между подготовкой байтов первого и его
    замком: сама замена принятой базы идёт под замком индекса, и вклиниться
    туда уже нельзя.
    """
    from gc_core.agent_edits import snapshots

    root = tmp_path / "diffs"
    writer_a = SnapshotStore(root, "cli_th1")
    writer_b = SnapshotStore(root, "cli_th1")
    real_lock = snapshots.file_lock
    interleaved = []

    def interleaving_lock(*args, **kwargs):
        if not interleaved:
            interleaved.append(True)
            # второй писатель проходит ЦЕЛИКОМ, пока первый держит готовые байты
            writer_b.capture_accepted("a.py", b"B" * 8, head_oid=None)
        return real_lock(*args, **kwargs)

    monkeypatch.setattr(snapshots, "file_lock", interleaving_lock)

    writer_a.capture_accepted("a.py", b"A" * 8, head_oid=None)

    assert interleaved, "перехват не сработал — тест ничего не проверил"
    state = writer_a.accepted_state("a.py")
    assert state is not None and state.content == b"A" * 8


#: Сколько знаков имя временного файла вправе прибавить к имени снимка.
#: Не вкусовщина, а бюджет пути: имя снимка — `<sha256>_<system>_<thread>.orig`,
#: это уже около 127 знаков фиксированного хвоста, а предел пути на Windows
#: без `LongPathsEnabled` — 260. Каждый знак хвоста временного файла срезает
#: глубину каталога проекта, на которой снимки ещё пишутся; отказ записи
#: рекордер глотает, и пользователь получает мёртвый откат молча.
#: `.{8 hex}.tmp` стоит 13 знаков, 16 оставлены как небольшой запас.
_TMP_NAME_BUDGET = 16


def test_tmp_name_stays_within_the_path_budget(tmp_path, monkeypatch):
    """Уникальность временного имени не имеет права съедать бюджет пути."""
    from gc_core.agent_edits import snapshots

    seen: list[str] = []
    real_replace = snapshots.atomic_replace

    def spying_replace(src, dst):
        seen.append(Path(src).name)
        real_replace(src, dst)

    monkeypatch.setattr(snapshots, "atomic_replace", spying_replace)

    store = SnapshotStore(tmp_path / "diffs", "cli_a-very-real-thread-id")
    store.capture_session("pkg/module.py", b"x")

    orig = next(p for p in store.root.iterdir() if p.suffix == ".orig")
    tmp_name = next(name for name in seen if name.startswith(orig.name))
    assert len(tmp_name) - len(orig.name) <= _TMP_NAME_BUDGET


# --- отложенная запись индекса (пакетная запись шелл-канала) -------------------


def test_deferred_index_writes_the_index_once_for_a_whole_batch(tmp_path, monkeypatch):
    """Внутри `deferred_index` отметки копятся в памяти — индекс пишется ОДИН раз.

    Индекс переписывается целиком на каждой отметке, а растёт с числом путей:
    команда, тронувшая тысячи файлов, переписывала его по два раза на файл —
    квадратичная цена (замер: 2000 файлов — 20 с из 51 под профайлером)."""
    store = _store(tmp_path)
    saves = []
    real_save = store._save_index
    monkeypatch.setattr(store, "_save_index", lambda index: (saves.append(1), real_save(index)))

    with store.deferred_index():
        for i in range(5):
            store.capture_session(f"src/f{i}.py", b"base", head_rev="rev1")
            store.capture_turn(f"src/f{i}.py", b"base", "turn-1")
        store.mark_created("src/new.py", head_rev="rev1")
        assert saves == []

    assert saves == [1]


def test_deferred_marks_are_visible_inside_the_block_and_after_it(tmp_path):
    """Чтения внутри блока видят ещё не записанные отметки, после — все на диске.

    Идемпотентность захвата стоит на чтении индекса: `.turn` того же хода второй
    раз не пишется, отметка создания не ставится поверх точного снимка. Не видя
    своих же отложенных отметок, пакет перезаписал бы базы хода."""
    store = _store(tmp_path)
    with store.deferred_index():
        store.capture_session("src/a.py", b"base", head_rev="rev1")
        store.capture_turn("src/a.py", b"base", "turn-1")
        store.capture_turn("src/a.py", b"later", "turn-1")      # тот же ход — no-op
        store.mark_created("src/a.py", head_rev="rev1")          # точный снимок уже есть
        assert store.session_head_rev("src/a.py") == "rev1"
        assert store.has_turn_baseline("src/a.py", "turn-1")
        assert store.created_head_rev("src/a.py") is None

    fresh = _store(tmp_path)
    assert fresh.session_head_rev("src/a.py") == "rev1"
    assert fresh.turn_baseline("src/a.py", "turn-1") == b"base"
    assert fresh.created_head_rev("src/a.py") is None


def test_deferred_flush_keeps_a_concurrent_writers_marks(tmp_path):
    """Сброс накладывает отметки на СВЕЖИЙ индекс, а не пишет снятую копию.

    Пока идёт пакет, индекс вправе писать другой писатель того же треда
    (открытый TUI принимает файл, `gc-cli` по тому же треду). Запись копией,
    снятой в начале, молча стёрла бы его отметку — и принятый файл потерял бы
    свою ступень."""
    store = _store(tmp_path)
    other = _store(tmp_path)
    with store.deferred_index():
        store.capture_session("src/a.py", b"base", head_rev="rev1")
        other.capture_accepted("src/b.py", b"accepted", head_oid=None)
    fresh = _store(tmp_path)
    assert fresh.session_head_rev("src/a.py") == "rev1"
    assert fresh.accepted_state("src/b.py") is not None


def test_zero_cap_disables_eviction_but_not_the_cleanup_of_dead_marks(tmp_path):
    """`snapshots_max_files: 0` — «не вытеснять», но мёртвые отметки индекса всё равно убираются.

    Уборка отметок мёртвых тредов — не вытеснение: у отметки создания файла нет по
    построению, и без уборки index.json рос бы весь срок жизни проекта."""
    diffs_root = tmp_path / "diffs"
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir(parents=True)
    edits_journal_path(str(threads_dir), "sys", "th").write_text("", encoding="utf-8")
    store_alive = SnapshotStore(diffs_root, "sys_th", threads_dir)
    store_dead = SnapshotStore(diffs_root, "sys_other", threads_dir)
    store_alive.mark_created("a.py", head_rev="a" * 40)
    store_dead.mark_created("b.py", head_rev="b" * 40)
    store_dead.capture_session("c.py", b"old")          # файл мёртвого треда

    store_alive.evict_lru(max_files=0)

    assert store_alive.created_head_rev("a.py") == "a" * 40
    assert store_dead.created_head_rev("b.py") is None     # мёртвая отметка убрана
    assert store_dead.session_baseline("c.py") == b"old"   # а вытеснения нет


def _busy_lock(monkeypatch) -> None:
    """Замок индекса держит другой писатель — захват отвечает таймаутом."""
    from gc_core.agent_edits import snapshots

    def busy(*_a, **_k):
        raise TimeoutError("индекс снимков занят")

    monkeypatch.setattr(snapshots, "file_lock", busy)


@pytest.mark.parametrize("operation", ["accept_absence", "clear"])
def test_a_failed_accepted_change_leaves_the_accepted_state_untouched(
    tmp_path, monkeypatch, operation
):
    """Изменение принятой базы, упавшее на замке, не меняет её вовсе.

    Байты `.accepted` удалялись ДО записи индекса: таймаут замка оставлял
    индекс «принято с содержимым» без самих байтов — ступень терялась, откат
    уходил ниже неё, а экран говорил «не изменилось». Сначала индекс, потом
    удаление: упало — не изменилось ничего."""
    store = _store(tmp_path)
    store.capture_accepted("a.py", b"accepted X", head_oid=None)
    _busy_lock(monkeypatch)

    with pytest.raises(TimeoutError):
        if operation == "accept_absence":
            store.capture_accepted("a.py", None, head_oid=None)
        else:
            store.clear_accepted("a.py")

    monkeypatch.undo()
    assert _store(tmp_path).accepted_state("a.py").content == b"accepted X"


def test_exact_base_is_not_taken_over_a_counting_one(tmp_path):
    """Точная база не ложится поверх счётной — ни сессии, ни того же хода.

    Счётная база значит: путь уже правила шелл-команда, а точного «до» у неё не
    было (путь был грязен). Снимок, снятый позже — тулом, — это состояние ПОСЛЕ
    команды: записанный в `.orig`, он выдал бы промежуточное состояние за начало
    сессии, и откат «к началу» оставил бы правки команды на диске. Правило одно
    для обоих каналов (карманный подканал держал его сам)."""
    store = _store(tmp_path)
    store.capture_shell_session("a.py", b"index state")
    store.capture_shell_turn("a.py", b"index state", "t1")

    assert store.capture_session("a.py", b"after command") is False
    store.capture_turn("a.py", b"after command", "t1")
    store.capture_turn("a.py", b"turn two start", "t2")

    assert store.session_baseline("a.py") is None
    assert store.turn_baseline("a.py", "t1") is None
    assert store.turn_baseline("a.py", "t2") == b"turn two start"   # другой ход — своё «до»


def test_a_failed_byte_replace_keeps_the_previous_acceptance(tmp_path, monkeypatch):
    """Не легли новые байты принятия — прежнее принятие остаётся целиком.

    Индекс пишется первым; если замена байтов следом падает, а прежнее
    принятие было удалением (байтов нет), индекс «принято с содержимым» без
    байтов терял ступень, и откат уходил ниже неё. Упавшая замена откатывает
    индекс: либо принятие целиком, либо ничего."""
    from gc_core.agent_edits import snapshots

    store = _store(tmp_path)
    store.capture_accepted("a.py", None, head_oid="old")

    real_replace = snapshots.atomic_replace

    def broken_bytes(src, dst):
        if str(dst).endswith(".accepted"):         # индекс пишется, байты — нет
            raise OSError("диск отказал")
        real_replace(src, dst)

    monkeypatch.setattr(snapshots, "atomic_replace", broken_bytes)
    with pytest.raises(OSError):
        store.capture_accepted("a.py", b"new bytes", head_oid="new")
    monkeypatch.undo()

    state = _store(tmp_path).accepted_state("a.py")
    assert state is not None and state.content is None and state.head_oid == "old"
    assert not list((tmp_path / "diffs").glob("*.tmp"))    # временный файл убран
