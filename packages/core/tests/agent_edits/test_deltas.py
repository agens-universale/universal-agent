"""Итоговая дельта файла: снимок «до» против состояния на диске.

Общий расчёт для сводки хода и экрана обзора: не сумма пошаговых счётчиков
журнала, а разница между снимком и тем, что лежит на диске сейчас.
"""
from __future__ import annotations

from pathlib import Path

from gc_core.agent_edits.deltas import (
    EMPTY_DELTA,
    abs_path_for_key,
    build_diff,
    delta_vs_disk,
    file_entries,
    net_summary,
)
from gc_core.agent_edits.journal import EditRecord
from gc_core.utils.file_text import sha256_hex_of_bytes


def _rec(
    path="a.py", op="edit", add=1, del_=1, flags=None,
    *, ts=1.0, call_id="c1", before_sha256=None, after_sha256=None,
):
    return EditRecord(
        path=path, turn_id="t1", ts=ts, call_id=call_id, op=op,
        hunks="", add=add, del_=del_,
        before_sha256=before_sha256, after_sha256=after_sha256,
        flags=list(flags or []),
    )


def _sha(data: bytes) -> str:
    return sha256_hex_of_bytes(data)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


# --- delta_vs_disk ----------------------------------------------------------


def test_delta_counts_snapshot_against_disk(tmp_path):
    disk = _write(tmp_path / "a.py", "a\nB\nc\n")

    delta = delta_vs_disk(b"a\nb\nc\n", disk)

    assert (delta.add, delta.del_) == (1, 1)
    assert "+B" in delta.hunks


def test_delta_without_snapshot_counts_whole_file_as_added(tmp_path):
    disk = _write(tmp_path / "new.py", "one\ntwo\nthree\n")

    delta = delta_vs_disk(None, disk)

    assert (delta.add, delta.del_) == (3, 0)


def test_delta_for_missing_file_counts_whole_snapshot_as_deleted(tmp_path):
    delta = delta_vs_disk(b"one\ntwo\n", tmp_path / "gone.py")

    assert (delta.add, delta.del_) == (0, 2)


def test_delta_of_untouched_file_is_zero(tmp_path):
    disk = _write(tmp_path / "a.py", "a\nb\n")

    assert delta_vs_disk(b"a\nb\n", disk) == EMPTY_DELTA


def test_delta_zero_when_disk_side_exceeds_cap(tmp_path):
    disk = _write(tmp_path / "big.py", "x\n" * 100)

    assert delta_vs_disk(b"", disk, max_source_bytes=10) == EMPTY_DELTA


def test_delta_zero_when_snapshot_side_exceeds_cap(tmp_path):
    disk = _write(tmp_path / "a.py", "x\n")

    assert delta_vs_disk(b"y\n" * 100, disk, max_source_bytes=10) == EMPTY_DELTA


def test_delta_zero_for_binary_disk_content(tmp_path):
    disk = tmp_path / "a.bin"
    disk.write_bytes(b"\x00\x01\x02binary\x00")

    assert delta_vs_disk(b"text\n", disk) == EMPTY_DELTA


def test_delta_decodes_non_utf8_snapshot_like_recorder(tmp_path):
    """Контент хранится байтами — декод best-effort, как в рекордере."""
    disk = tmp_path / "a.txt"
    disk.write_bytes("привет\nмир\n".encode("cp1251"))

    delta = delta_vs_disk("привет\nмир\n".encode("cp1251"), disk)

    assert delta == EMPTY_DELTA


def test_identical_bytes_are_countable_even_when_undecodable(tmp_path):
    """Дефект диагностики: равенство байтов решается ДО декодирования.

    Короткий кириллический текст в cp1251 chardet распознаёт с уверенностью
    0.145 — ниже порога 0.5, и обе стороны best-effort декодера объявляются
    None. Без быстрого пути по равенству байтов это превращает побайтово
    идентичные базу и диск в «несчитаемые» (``countable=False``), хотя ответ
    «разницы нет» известен без всякого декодирования.
    """
    content = "привет\n".encode("cp1251")
    disk = tmp_path / "a.txt"
    disk.write_bytes(content)

    delta = delta_vs_disk(content, disk)

    assert (delta.countable, delta.add, delta.del_) == (True, 0, 0)


# --- abs_path_for_key -------------------------------------------------------


def test_abs_path_for_relative_key_is_under_launch_root(tmp_path):
    assert abs_path_for_key(str(tmp_path), "pkg/a.py") == tmp_path / "pkg" / "a.py"


def test_abs_path_for_absolute_key_kept_as_is(tmp_path):
    key = (tmp_path / "outside" / "a.py").as_posix()

    assert abs_path_for_key(str(tmp_path / "root"), key) == Path(key)


# --- net_summary ------------------------------------------------------------


def test_net_summary_sums_per_file_not_per_record(tmp_path):
    _write(tmp_path / "a.py", "a\nB\nc\n")
    _write(tmp_path / "b.py", "one\ntwo\n")
    baselines = {"a.py": b"a\nb\nc\n", "b.py": b"one\n"}
    records = [
        _rec(path="a.py", add=50, del_=50),
        _rec(path="a.py", add=50, del_=50),
        _rec(path="b.py", add=7, del_=0),
    ]

    assert net_summary(records, baselines.get, str(tmp_path)) == (2, 2, 1)


def test_net_summary_counts_no_diff_file_without_numbers(tmp_path):
    _write(tmp_path / "a.bin", "whatever\n")

    summary = net_summary(
        [_rec(path="a.bin", op="binary", add=0, del_=0, flags=["no_diff"])],
        lambda key: None,
        str(tmp_path),
    )

    assert summary == (1, 0, 0)


def test_net_summary_uses_file_with_one_real_record_among_no_diff(tmp_path):
    _write(tmp_path / "a.py", "a\nb\n")

    summary = net_summary(
        [
            _rec(path="a.py", op="shell", add=0, del_=0, flags=["no_diff"]),
            _rec(path="a.py", add=1, del_=0),
        ],
        lambda key: b"a\n",
        str(tmp_path),
    )

    assert summary == (1, 1, 0)


def test_net_summary_empty_records(tmp_path):
    assert net_summary([], lambda key: None, str(tmp_path)) == (0, 0, 0)


# --- net_summary: снимка нет — база пуста только у создания ------------------


def test_net_summary_shell_edit_without_snapshot_takes_journal_counters(tmp_path):
    """Команда переформатировала существующий файл: снимка нет, но и создан он
    в этом ходе не был.

    Шелл-канал снимков не снимает никогда. Пустая база наврала бы весь файл в
    добавленные (+500 −0) — правда лежит в счётчиках журнала.
    """
    _write(tmp_path / "fmt.py", "".join(f"line{i}\n" for i in range(500)))

    summary = net_summary(
        [_rec(path="fmt.py", op="shell", add=12, del_=12)],
        lambda key: None,
        str(tmp_path),
    )

    assert summary == (1, 12, 12)


def test_net_summary_partial_append_takes_journal_counters(tmp_path):
    """`partial` (append сверх капа) съём снимка глушит — считаем по журналу.

    Дописали 3 строки в лог на 10000 строк: от пустой базы вышло бы +10000.
    """
    _write(tmp_path / "big.log", "".join(f"line{i}\n" for i in range(10_000)))

    summary = net_summary(
        [_rec(path="big.log", op="append", add=3, del_=0, flags=["partial"])],
        lambda key: None,
        str(tmp_path),
    )

    assert summary == (1, 3, 0)


def test_net_summary_created_file_without_snapshot_counts_whole_file(tmp_path):
    """`op="create"` — единственный случай, когда пустая база легальна."""
    _write(tmp_path / "new.py", "one\ntwo\nthree\n")

    summary = net_summary(
        [_rec(path="new.py", op="create", add=3, del_=0)],
        lambda key: None,
        str(tmp_path),
    )

    assert summary == (1, 3, 0)


def test_net_summary_create_after_earlier_record_is_not_an_empty_baseline(tmp_path):
    """Создание ПОСЛЕ более ранней записи по тому же пути пустой базы не даёт.

    Команда снесла файл и пересоздала его fs-тулом: до хода файл был, и его
    содержимое в базу «пусто» не превращается — цифры из журнала.
    """
    _write(tmp_path / "a.py", "one\ntwo\n")

    summary = net_summary(
        [
            _rec(path="a.py", op="delete", add=0, del_=9),
            _rec(path="a.py", op="create", add=2, del_=0),
        ],
        lambda key: None,
        str(tmp_path),
    )

    assert summary == (1, 2, 9)


def test_net_summary_mixes_snapshot_and_journal_paths_in_one_turn(tmp_path):
    """Смешанный ход: путь со снимком считается по диску, путь без — по журналу."""
    _write(tmp_path / "a.py", "a\nB\nc\n")
    _write(tmp_path / "b.py", "".join(f"line{i}\n" for i in range(200)))
    baselines = {"a.py": b"a\nb\nc\n"}

    summary = net_summary(
        [
            _rec(path="a.py", add=50, del_=50),
            _rec(path="b.py", op="shell", add=4, del_=1),
        ],
        baselines.get,
        str(tmp_path),
    )

    assert summary == (2, 5, 2)


# --- file_entries: счётчики и честность пометок ------------------------------


def test_file_entries_marks_inexact_when_baseline_missing(tmp_path):
    """Правка командой без базы: счётчики из журнала, пометка «неточно»."""
    _write(tmp_path / "a.py", "x\n")

    [entry] = file_entries(
        [_rec("a.py", op="shell", add=5, del_=1, call_id=None)],
        lambda key: None,
        str(tmp_path),
    )

    assert (entry.exact, entry.add, entry.del_, entry.by_shell) == (False, 5, 1, True)


def test_file_entries_exact_uses_disk_delta_not_record_sum(tmp_path):
    """База есть — итог по диску, а не сумма пошаговых счётчиков записей."""
    _write(tmp_path / "a.py", "a\nB\nc\n")

    [entry] = file_entries(
        [_rec("a.py", add=50, del_=50), _rec("a.py", add=50, del_=50)],
        lambda key: b"a\nb\nc\n",
        str(tmp_path),
    )

    assert (entry.exact, entry.add, entry.del_, entry.kind) == (True, 1, 1, "modified")


def test_shell_record_with_counting_base_is_not_revertable(tmp_path):
    """Счётная база из индекса git считает, но откату не даёт права."""
    _write(tmp_path / "a.py", "new\n")

    [entry] = file_entries(
        [_rec("a.py", op="shell", call_id=None)],
        lambda key: b"old\n",
        str(tmp_path),
        revertable_for=lambda key: False,
    )

    assert entry.exact is True and entry.revertable is False


def test_tool_record_with_precise_snapshot_is_revertable(tmp_path):
    _write(tmp_path / "a.py", "a\nB\n")

    [entry] = file_entries(
        [_rec("a.py")],
        lambda key: b"a\nb\n",
        str(tmp_path),
        revertable_for=lambda key: True,
    )

    assert (entry.revertable, entry.by_shell) == (True, False)


def test_created_file_on_disk_is_revertable_without_predicate(tmp_path):
    """Созданный в наборе файл откатывается удалением — снимок для этого не нужен."""
    _write(tmp_path / "new.py", "one\n")

    [entry] = file_entries(
        [_rec("new.py", op="create", add=1, del_=0)],
        lambda key: None,
        str(tmp_path),
    )

    assert (entry.kind, entry.exact, entry.add, entry.revertable) == (
        "created", True, 1, True,
    )


def test_created_file_ignores_later_snapshot_and_counts_whole_file(tmp_path):
    """У созданного файла база набора пуста ВСЕГДА — снимок тут из середины.

    Рекордер снимает `.orig`, только когда старый контент был, то есть на
    ВТОРОЙ правке: тул создал файл, потом дописал строку. Победи такой снимок
    пустоту — в кумулятиве осталась бы одна дописанная строка вместо всего
    файла, который агент только что сочинил.
    """
    _write(tmp_path / "new.py", "one\ntwo\nthree\n")
    calls: list[str] = []

    [entry] = file_entries(
        [
            _rec("new.py", op="create", add=2, del_=0),
            _rec("new.py", op="edit", add=1, del_=0),
        ],
        lambda key: calls.append(key) or b"one\ntwo\n",
        str(tmp_path),
    )

    assert (entry.exact, entry.add, entry.del_) == (True, 3, 0)
    assert calls == []   # снимок созданного файла даже не читается


# --- file_entries: ступень выше отменяет created-обход ------------------------


def test_created_with_step_counts_from_step_and_is_modified(tmp_path):
    (tmp_path / "new.py").write_text("v2\n", encoding="utf-8")
    recs = [_rec("new.py", op="create"), _rec("new.py", op="edit")]
    [e] = file_entries(recs, lambda k: b"v1\n", str(tmp_path),
                       stepped=lambda k: True)
    assert (e.kind, e.revertable) == ("modified", True)
    assert (e.add, e.del_) == (1, 1)          # дельта от ступени, не «весь файл добавлен»


def test_created_without_step_keeps_wave3_contract(tmp_path):
    calls = []
    recs = [_rec("new.py", op="create")]
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")
    [e] = file_entries(recs, lambda k: calls.append(k), str(tmp_path), stepped=None)
    assert calls == [] and e.kind == "created"


def test_created_and_deleted_ignores_snapshot_and_is_not_revertable(tmp_path):
    """Снимок из середины набора не делает откат созданного файла возможным.

    Иначе откат СОЗДАЛ бы файл с содержимым, которого у пользователя никогда
    не было: до набора файла не существовало вовсе.
    """
    recs = [_rec("gone.py", op="create"), _rec("gone.py", op="delete")]

    [entry] = file_entries(
        recs,
        lambda key: b"one\ntwo\n",
        str(tmp_path),
        revertable_for=lambda key: True,
    )

    assert (entry.kind, entry.add, entry.del_, entry.revertable) == (
        "deleted", 0, 0, False,
    )


def test_deleted_file_counts_snapshot_as_removed_and_stays_revertable(tmp_path):
    [entry] = file_entries(
        [_rec("gone.py", op="delete", add=0, del_=9)],
        lambda key: b"one\ntwo\n",
        str(tmp_path),
        revertable_for=lambda key: True,
    )

    assert (entry.kind, entry.add, entry.del_, entry.revertable) == (
        "deleted", 0, 2, True,
    )


def test_no_diff_paths_never_call_baseline_for(tmp_path):
    """`session_baseline` читает снимок целиком — на бинарях это трата диска."""
    calls: list[str] = []

    file_entries(
        [_rec("b.bin", op="binary", flags=["no_diff"])],
        lambda key: calls.append(key) or None,
        str(tmp_path),
    )

    assert calls == []


def test_no_diff_path_is_listed_with_zero_counters(tmp_path):
    """Путь без диффа в списке есть, но счётчиков журнала себе не берёт."""
    _write(tmp_path / "a.bin", "whatever\n")

    [entry] = file_entries(
        [_rec("a.bin", op="binary", add=9, del_=9, flags=["no_diff"])],
        lambda key: None,
        str(tmp_path),
        revertable_for=lambda key: True,
    )

    assert (entry.add, entry.del_, entry.exact, entry.kind) == (0, 0, False, "modified")
    assert entry.countable is False   # дельту считать было нечем — нули не «точный ноль»
    assert entry.revertable is True   # доступность отката — по снимку, не по op


def test_oversize_path_is_not_countable_and_takes_journal_counters(tmp_path):
    """Сравнение отказало по капу: нули соврали бы «файл не изменился»."""
    _write(tmp_path / "big.py", "".join(f"line{i}\n" for i in range(100)))

    [entry] = file_entries(
        [_rec("big.py", add=7, del_=2)],
        lambda key: b"line0\n",
        str(tmp_path),
        max_source_bytes=10,
    )

    assert (entry.countable, entry.exact, entry.add, entry.del_) == (False, False, 7, 2)


def test_countable_path_says_so(tmp_path):
    _write(tmp_path / "a.py", "a\nB\n")

    [entry] = file_entries([_rec("a.py")], lambda key: b"a\nb\n", str(tmp_path))

    assert (entry.countable, entry.exact) == (True, True)


def test_journal_fallback_without_base_stays_countable(tmp_path):
    """Р12 (базы нет) и «считать нечем» — разные состояния, не одно."""
    _write(tmp_path / "a.py", "x\n")

    [entry] = file_entries(
        [_rec("a.py", op="shell", add=5, del_=1, call_id=None)],
        lambda key: None,
        str(tmp_path),
    )

    assert (entry.exact, entry.countable) == (False, True)


def test_line_separator_inside_a_line_is_not_a_line_break(tmp_path):
    """`\u2028` внутри строки — символ, а не конец строки.

    `str.splitlines` считает его разделителем, git — нет. Разойдясь, счётчик
    показывал бы на строку больше, а номера в хэнках уезжали бы (пятый раунд
    ревью, находка 3).
    """
    hunks, add, deleted = build_diff("one\n", "one\ntwo\u2028three\n", 3)

    assert (add, deleted) == (1, 0)


def test_file_entries_detects_external_edit(tmp_path):
    recs = [
        _rec("a.py", after_sha256="h1"),
        _rec("a.py", before_sha256="ЧУЖОЙ", after_sha256="h2"),
    ]

    assert file_entries(recs, lambda key: b"", str(tmp_path))[0].external is True


def test_file_entries_none_hash_is_not_a_break(tmp_path):
    """`None` — «сравнить не с чем», а не разрыв цепочки (§3 спеки)."""
    recs = [
        _rec("a.py", after_sha256=None),
        _rec("a.py", before_sha256=None, after_sha256="h2"),
        _rec("a.py", before_sha256="h2", after_sha256=None),
    ]

    assert file_entries(recs, lambda key: b"", str(tmp_path))[0].external is False


def test_created_and_deleted_in_session_is_noop(tmp_path):
    recs = [_rec("gone.py", op="create"), _rec("gone.py", op="delete")]

    [entry] = file_entries(recs, lambda key: None, str(tmp_path))

    assert (entry.kind, entry.add, entry.del_, entry.revertable) == (
        "deleted", 0, 0, False,
    )


def test_file_entries_sorted_by_last_edit_desc(tmp_path):
    recs = [
        _rec("old.py", ts=1.0),
        _rec("fresh.py", ts=5.0),
        _rec("old.py", ts=2.0),
    ]

    entries = file_entries(recs, lambda key: None, str(tmp_path))

    assert [e.path for e in entries] == ["fresh.py", "old.py"]
    assert [e.last_ts for e in entries] == [5.0, 2.0]


def test_net_summary_equals_aggregate_of_file_entries(tmp_path):
    """Р11: сводка — тонкий агрегат над пофайловым расчётом, не второй расчёт."""
    _write(tmp_path / "a.py", "a\nB\nc\n")
    _write(tmp_path / "b.py", "".join(f"line{i}\n" for i in range(200)))
    _write(tmp_path / "c.bin", "binary\n")
    _write(tmp_path / "new.py", "one\ntwo\n")
    baselines = {"a.py": b"a\nb\nc\n"}
    recs = [
        _rec("a.py", add=50, del_=50),
        _rec("b.py", op="shell", add=4, del_=1, call_id=None),
        _rec("c.bin", op="binary", add=0, del_=0, flags=["no_diff"]),
        _rec("new.py", op="create", add=2, del_=0),
    ]

    entries = file_entries(recs, baselines.get, str(tmp_path))

    assert net_summary(recs, baselines.get, str(tmp_path)) == (
        len(entries),
        sum(e.add for e in entries),
        sum(e.del_ for e in entries),
    )


# --- потолок усечения: ноль означает «без лимита» ----------------------------


def test_zero_max_chars_means_no_limit():
    """Оба поставляемых шаблона обещают `max_diff_chars: 0 = без лимита`.

    Без этой проверки ноль резал бы каждый хэнк в пустую строку и помечал
    запись усечённой — журнал терял бы содержимое ровно там, где просили не
    ограничивать. Проверка однажды уже терялась при переписывании, поэтому
    владелец у неё один на все три точки усечения.
    """
    from gc_core.agent_edits.deltas import truncate_hunks

    hunks = "@@ -1 +1 @@" + "x" * 500
    assert truncate_hunks(hunks, 0) == (hunks, [])
    assert truncate_hunks(hunks, -1) == (hunks, [])


def test_positive_max_chars_truncates_and_flags():
    from gc_core.agent_edits.deltas import truncate_hunks

    assert truncate_hunks("abcdef", 3) == ("abc", ["truncated"])
    assert truncate_hunks("abc", 3) == ("abc", [])


def test_foreign_content_on_disk_is_external_before_the_next_record(tmp_path):
    """Правка мимо агента видна СРАЗУ, не дожидаясь следующей записи журнала.

    Счётчики строки идут от базы против ДИСКА, поэтому чужая строка попадает
    в `+N` немедленно. Пока признак читался из одного журнала, экран показывал
    это содержимое как правку агента — ложное авторство (смок 2026-09-10).
    """
    _write(tmp_path / "a.py", "AGENT\nЧУЖАЯ СТРОКА\n")
    recs = [_rec("a.py", before_sha256=_sha(b"OLD\n"),
                 after_sha256=_sha("AGENT\n".encode()))]

    assert file_entries(recs, lambda key: b"OLD\n", str(tmp_path))[0].external is True


def test_disk_left_by_the_agent_is_not_external(tmp_path):
    """Диск ровно такой, каким его оставил агент, — чужого тут нет."""
    _write(tmp_path / "a.py", "AGENT\n")
    recs = [_rec("a.py", before_sha256=_sha(b"OLD\n"),
                 after_sha256=_sha("AGENT\n".encode()))]

    assert file_entries(recs, lambda key: b"OLD\n", str(tmp_path))[0].external is False


def test_disk_returned_to_the_baseline_is_not_external(tmp_path):
    """После отката на диске ровно база — и это состояние наше.

    Запись шелл-канала: `before_sha256` у неё нет вовсе, поэтому хэша базы в
    цепочке журнала не встретится. Без явной подачи базы собственный откат
    снова выглядел бы чужой правкой — тот самый дефект, ради которого правился
    предикат.
    """
    _write(tmp_path / "a.py", "OLD\n")
    recs = [_rec("a.py", before_sha256=None, after_sha256=_sha("AGENT\n".encode()))]

    assert file_entries(recs, lambda key: b"OLD\n", str(tmp_path))[0].external is False


def test_unknown_last_write_cannot_claim_the_disk_is_foreign(tmp_path):
    """Чем агент кончил — неизвестно (`after_sha256 is None`), и судить нечем (§3)."""
    _write(tmp_path / "a.py", "ЧТО УГОДНО\n")
    recs = [_rec("a.py", before_sha256=_sha(b"OLD\n"), after_sha256=None)]

    assert file_entries(recs, lambda key: b"OLD\n", str(tmp_path))[0].external is False


def test_return_to_a_known_state_is_not_an_external_edit(tmp_path):
    """Откат вернул файл в состояние, которое в цепочке уже было.

    Откат пишет на диск, но записи в журнал не делает, и следующая правка
    агента честно говорит «видел то, что было раньше». Прежняя форма считала
    это разрывом и метила путь «извне» — про действие, совершённое на этом же
    экране (смок 2026-09-10).
    """
    recs = [
        _rec("a.py", before_sha256="h0", after_sha256="h1"),
        _rec("a.py", before_sha256="h0", after_sha256="h1"),   # после отката к h0
    ]

    assert file_entries(recs, lambda key: b"", str(tmp_path))[0].external is False


def test_return_to_the_baseline_after_a_shell_edit_is_not_an_external_edit(tmp_path):
    """Откат к БАЗЕ после правки команды — не «извне», даже если «до» команды неизвестно.

    Смок 2026-09-24: команда правит файл (git-подканал «до» не знает — `before`
    пустой), откат возвращает его к базе сессии, команда правит снова — теперь
    карманный подканал знает «до», и это байты базы. Цепочка их не видела ни
    разу: база, от которой идёт отсчёт, в известные не входила, и 100 путей
    из 500 получили «извне» за правки самого агента."""
    base, edited = b"x_3 = 3\n", b"x_3 = 33\n"
    (tmp_path / "a.py").write_bytes(edited)
    recs = [
        _rec("a.py", ts=1.0, call_id=None, op="shell", before_sha256=None,
             after_sha256=_sha(edited)),
        _rec("a.py", ts=2.0, call_id=None, op="shell", before_sha256=_sha(base),
             after_sha256=_sha(edited)),                  # после отката к базе
    ]

    assert file_entries(recs, lambda key: base, str(tmp_path))[0].external is False


# Цепочка одного пути живыми байтами: база сессии, правка агента, ручная правка
# поверх неё, и ещё две правки агента сверху.
_B = b"base\n"
_A1 = _B + b"agent1\n"
_M = _A1 + b"mine\n"            # эту строку агент не писал
_A2 = _M + b"agent2\n"
_A3 = _A2 + b"agent3\n"


def _chain(path="a.py"):
    return [
        _rec(path, ts=1.0, before_sha256=_sha(_B), after_sha256=_sha(_A1)),
        _rec(path, ts=2.0, before_sha256=_sha(_M), after_sha256=_sha(_A2)),
        _rec(path, ts=3.0, before_sha256=_sha(_A2), after_sha256=_sha(_A3)),
    ]


def test_external_mark_holds_while_the_foreign_lines_are_still_shown(tmp_path):
    """Пометка держится, пока чужие строки в ПОКАЗАННОЙ дельте.

    Прежняя форма («решает последняя сравнимая запись») гасила признак, как
    только агент правил файл ещё раз поверх чужой правки. Строка `mine` при
    этом оставалась в дельте от базы, счётчики её включали, а предупреждение
    перед откатом исчезало — то есть `r` уничтожал ручную работу молча. Тот же
    класс, что находка смока про `git restore` (пятый раунд ревью, находка 3).
    """
    _write(tmp_path / "a.py", _A3.decode())

    [entry] = file_entries(_chain(), lambda key: _B, str(tmp_path))

    assert entry.external is True
    assert "mine" in (tmp_path / "a.py").read_text(encoding="utf-8")


def test_external_mark_clears_when_the_base_moved_past_the_foreign_edit(tmp_path):
    """Уходит она не от новой правки, а от сдвига БАЗЫ.

    Приняли состояние, в котором чужая строка уже есть, — она перестала быть
    частью показанной дельты и стала точкой отсчёта. История ниже базы к делу
    больше не относится, и признак её не читает.
    """
    _write(tmp_path / "a.py", _A3.decode())

    [entry] = file_entries(_chain(), lambda key: _A2, str(tmp_path))

    assert entry.external is False


def test_history_below_the_base_is_not_read_at_all(tmp_path):
    """Обрезка идёт по ПОСЛЕДНЕЙ записи, чей `after` совпал с базой.

    База найдена в цепочке дважды — отсчёт ведётся от более поздней: ровно она
    и есть текущая точка, а всё до неё в дельту не входит.
    """
    recs = _chain() + [
        _rec("a.py", ts=4.0, before_sha256=_sha(_A3), after_sha256=_sha(_A2)),
        _rec("a.py", ts=5.0, before_sha256="ЧУЖОЙ", after_sha256=_sha(_A3)),
    ]
    _write(tmp_path / "a.py", _A3.decode())

    # база = _A2: последнее её вхождение — запись 4, значит читается только
    # запись 5, а она с разрывом.
    assert file_entries(recs, lambda key: _A2, str(tmp_path))[0].external is True


def test_external_key_does_not_reach_the_screen_or_the_counters(tmp_path):
    """Внешний файл считать нечем: базы у него нет и не будет.

    Строка без базы не имела бы права исчезнуть — доказать «правки не
    осталось» можно только сравнением с базой, — и висела бы на экране до
    конца сессии: ни принять, ни откатить, ни дождаться ухода.
    """
    launch_root = tmp_path / "proj"
    launch_root.mkdir()
    _write(launch_root / "own.py", "x\n")
    outside = _write(tmp_path / "outside" / "app.yml", "y\n")
    outside_key = outside.resolve().as_posix()

    records = [_rec(path="own.py"), _rec(path=outside_key)]

    entries = file_entries(records, lambda key: None, str(launch_root))
    assert [e.path for e in entries] == ["own.py"]

    files, _add, _del = net_summary(records, lambda key: None, str(launch_root))
    assert files == 1


def test_external_key_with_a_live_baseline_still_stays_off_the_screen(tmp_path):
    """Старый тред: абсолютный ключ и ЖИВОЙ снимок от прежней версии.

    Миграции у этой работы нет — согласованность даёт форма ключа. Значит путь,
    записанный прежним кодом по `write_guard.extra_paths` и имеющий настоящую
    базу, обязан уйти с экрана вместе с новыми. Фильтр не вправе смотреть на
    наличие базы: тогда старые треды вели бы себя иначе, чем новые.
    """
    launch_root = tmp_path / "proj"
    launch_root.mkdir()
    outside = _write(tmp_path / "outside" / "app.yml", "новое\n")
    outside_key = outside.resolve().as_posix()

    entries = file_entries(
        [_rec(path=outside_key)],
        lambda key: b"\xd1\x81\xd1\x82\xd0\xb0\xd1\x80\xd0\xbe\xd0\xb5\n",
        str(launch_root),
    )
    assert entries == []
