"""Юнит-тесты EditsLookup: поиск по call_id и сводка хода для UI.

Сводка хода считается по ИТОГУ: снимок начала хода против того, что лежит на
диске сейчас, — поэтому тесты кладут рядом с журналом настоящие файлы и
turn-снимки.
"""

from pathlib import Path

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_tui.services.edits_lookup import EditsLookup


def make_rec(
    path="a.py", turn="t1", call_id="c1", op="edit", add=1, del_=1, **kw
):
    base = dict(
        path=path,
        turn_id=turn,
        ts=1.0,
        call_id=call_id,
        op=op,
        hunks="@@ -1 +1 @@\n-a\n+b\n",
        add=add,
        del_=del_,
        before_sha256="x",
        after_sha256="y",
        flags=[],
    )
    base.update(kw)
    return EditRecord(**base)


class _Workspace:
    """Тред + его журнал, снапшоты и файлы на диске под launch_root."""

    def __init__(self, tmp_path: Path, **limits) -> None:
        self.threads_dir = tmp_path / "threads"
        self.threads_dir.mkdir()
        self.root = tmp_path / "proj"
        self.root.mkdir()
        self.journal = EditsJournal(
            edits_journal_path(str(self.threads_dir), "cli", "th1")
        )
        self.store = SnapshotStore(
            snapshots_root(str(self.root)), "cli_th1", self.threads_dir
        )
        self.lookup = EditsLookup(
            str(self.threads_dir),
            "cli",
            "th1",
            launch_root=str(self.root),
            **limits,
        )

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def snapshot(self, rel: str, text: str, turn: str = "t1") -> None:
        self.store.capture_turn(rel, text.encode("utf-8"), turn)


def make_lookup(tmp_path):
    ws = _Workspace(tmp_path)
    return ws.lookup, ws.journal


def test_call_id_seen_in_two_turns_gives_no_diff_at_all(tmp_path):
    """Один и тот же `call_id` в разных ходах — молчим, а не показываем чужое.

    Идентификатор приходит от провайдера как есть, уникальность его никто не
    обещал, а индекс строится на весь тред и оставляет последнюю запись. Значит
    под обычным чтением файла мог появиться дифф правки из другого хода — ложь
    о том, что агент сделал (пятый раунд ревью, находка 2).

    В настоящих журналах столкновений не нашлось, поэтому это защита, а не
    починка. Выбор между «не показать» и «показать чужое» очевиден: экран
    обязан молчать о том, чего не знает.
    """
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(path="a.py", turn="t1", call_id="call_0"))
    journal.append(make_rec(path="b.py", turn="t2", call_id="call_0"))

    assert lookup.by_call_id("call_0") is None


def test_call_id_repeated_inside_one_turn_still_resolves(tmp_path):
    """Один вызов правил несколько файлов — это НЕ двусмысленность.

    Тул за один вызов трогает много путей, записей столько же, и `call_id` у
    них общий. Ход один, ложного авторства тут нет: остаётся последняя запись,
    как и было.
    """
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(path="a.py", turn="t1", call_id="call_0"))
    journal.append(make_rec(path="b.py", turn="t1", call_id="call_0"))

    record = lookup.by_call_id("call_0")
    assert record is not None and record.path == "b.py"


def test_by_call_id_finds_record(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(call_id="c1"))

    rec = lookup.by_call_id("c1")

    assert rec is not None
    assert rec.call_id == "c1"


def test_by_call_id_missing_returns_none(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(call_id="c1"))

    assert lookup.by_call_id("nope") is None


def test_external_record_stays_visible_to_the_trace_index(tmp_path):
    """Лента — единственный потребитель, которому внешние записи нужны.

    Экран и счётчики их не видят (`deltas.file_entries`), а блок диффа
    собирается из хэнков по `call_id` и базы не требует. Фильтровать здесь
    значит выключить фичу целиком.
    """
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    outside_key = (tmp_path / "outside" / "app.yml").resolve().as_posix()

    EditsJournal(edits_journal_path(str(threads_dir), "cli", "th1")).append(
        make_rec(path=outside_key, call_id="c1", op="write")
    )

    lookup = EditsLookup(str(threads_dir), "cli", "th1")
    record = lookup.by_call_id("c1")

    assert record is not None
    assert record.path == outside_key
    assert record.hunks != ""


def test_shell_records_not_indexed_by_call_id(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(call_id=None, op="shell"))

    assert lookup.by_call_id(None) is None


def test_turn_summary_single_edit_matches_record_counters(tmp_path):
    """Регресс: файл, правленный один раз, считается ровно как раньше."""
    ws = _Workspace(tmp_path)
    ws.snapshot("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\n")
    ws.journal.append(
        make_rec(path="a.py", turn="t1", call_id="c1", add=1, del_=1)
    )

    assert ws.lookup.turn_summary("t1") == (1, 1, 1)


def test_turn_summary_three_rewrites_of_one_file_report_net_delta(tmp_path):
    """Живой случай: правка + две перезаписи целиком.

    Сумма пошаговых счётчиков давала «+293 −22» на файле в 133 строки —
    сводка обязана показать итог хода: 133 строки добавлено, 22 удалено.
    """
    ws = _Workspace(tmp_path)
    before = "".join(f"old{i}\n" for i in range(22))
    after = "".join(f"new{i}\n" for i in range(133))
    ws.snapshot("calc.py", before)
    ws.write("calc.py", after)
    ws.journal.append(
        make_rec(path="calc.py", turn="t1", call_id="c1", add=28, del_=22)
    )
    ws.journal.append(
        make_rec(
            path="calc.py",
            turn="t1",
            call_id=None,
            op="shell",
            add=133,
            del_=0,
        )
    )
    ws.journal.append(
        make_rec(
            path="calc.py",
            turn="t1",
            call_id=None,
            op="shell",
            add=132,
            del_=0,
        )
    )

    assert ws.lookup.turn_summary("t1") == (1, 133, 22)


def test_turn_summary_two_files_sums_per_file_deltas(tmp_path):
    ws = _Workspace(tmp_path)
    ws.snapshot("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\nd\n")  # +2 −1
    ws.snapshot("b.py", "one\ntwo\nthree\n")
    ws.write("b.py", "one\n")  # −2
    ws.journal.append(make_rec(path="a.py", turn="t1", call_id="c1"))
    ws.journal.append(make_rec(path="b.py", turn="t1", call_id="c2"))

    assert ws.lookup.turn_summary("t1") == (2, 2, 3)


def test_turn_summary_file_created_in_turn_counts_whole_file(tmp_path):
    """Снимать было нечего — файла до хода не было: весь файл добавлен.

    `op="create"` (его ставят fs-тулы, когда файла не существовало) — тот
    единственный случай, когда отсутствие снимка значит «база пуста»."""
    ws = _Workspace(tmp_path)
    ws.write("new.py", "one\ntwo\nthree\n")
    ws.journal.append(
        make_rec(
            path="new.py", turn="t1", call_id="c1", op="create", add=3, del_=0
        )
    )

    assert ws.lookup.turn_summary("t1") == (1, 3, 0)


def test_turn_summary_deleted_file_counts_whole_snapshot(tmp_path):
    ws = _Workspace(tmp_path)
    ws.snapshot("gone.py", "one\ntwo\nthree\nfour\n")
    ws.journal.append(
        make_rec(
            path="gone.py", turn="t1", call_id="c1", op="delete", add=0, del_=4
        )
    )

    assert ws.lookup.turn_summary("t1") == (1, 0, 4)


def test_turn_summary_no_diff_counts_as_file_not_in_sums(tmp_path):
    ws = _Workspace(tmp_path)
    ws.snapshot("a.py", "a\nb\nc\n")
    ws.write("a.py", "a\nB\nc\n")
    ws.write("b.bin", "мусор\n")
    ws.journal.append(make_rec(path="a.py", turn="t1", call_id="c1"))
    ws.journal.append(
        make_rec(
            path="b.bin",
            turn="t1",
            call_id="c2",
            add=0,
            del_=0,
            op="binary",
            flags=["no_diff"],
        )
    )

    assert ws.lookup.turn_summary("t1") == (2, 1, 1)


def test_turn_summary_includes_shell_records_alongside_fs_records(tmp_path):
    """Смешанный ход: каждый путь считается своим способом.

    a.py правил fs-тул — есть turn-снимок, считаем итог против диска (+1).
    b.sh переписала команда — снимка нет и создан он в этом ходе не был, цифры
    берём из журнала (+4 −2). c.py удалён — весь снимок в удалённые (−1).
    """
    ws = _Workspace(tmp_path)
    ws.snapshot("a.py", "a\n")
    ws.write("a.py", "a\nb\n")  # +1 по снимку
    ws.write("b.sh", "echo 1\necho 2\n")  # правлен командой: +4 −2 из журнала
    ws.snapshot("c.py", "gone\n")  # удалён: −1
    ws.journal.append(make_rec(path="a.py", turn="t1", call_id="c1"))
    ws.journal.append(
        make_rec(
            path="b.sh", turn="t1", call_id=None, op="shell", add=4, del_=2
        )
    )
    ws.journal.append(
        make_rec(
            path="c.py", turn="t1", call_id=None, op="delete", add=0, del_=1
        )
    )

    assert ws.lookup.turn_summary("t1") == (3, 5, 3)
    recs = ws.lookup.records_for_turn("t1")
    assert [r.path for r in recs] == ["a.py", "b.sh", "c.py"]


def test_turn_summary_shell_edit_of_existing_file_uses_journal_counters(
    tmp_path,
):
    """Живой случай: команда переформатировала файл на 500 строк, журнал +12 −12.

    Снимка у шелл-канала нет никогда, и пустая база выдала бы «+500 −0» —
    хуже, чем сумма записей до всей этой переделки.
    """
    ws = _Workspace(tmp_path)
    ws.write("fmt.py", "".join(f"line{i}\n" for i in range(500)))
    ws.journal.append(
        make_rec(
            path="fmt.py", turn="t1", call_id=None, op="shell", add=12, del_=12
        )
    )

    assert ws.lookup.turn_summary("t1") == (1, 12, 12)


def test_turn_summary_partial_append_uses_journal_counters(tmp_path):
    """Живой случай: `fs_append_file` дописал 3 строки в лог на 10000 строк.

    Флаг `partial` (append сверх капа) съём снимка глушит — от пустой базы
    вышло бы «+10000 −0».
    """
    ws = _Workspace(tmp_path)
    ws.write("big.log", "".join(f"line{i}\n" for i in range(10_000)))
    ws.journal.append(
        make_rec(
            path="big.log",
            turn="t1",
            call_id="c1",
            op="append",
            add=3,
            del_=0,
            flags=["partial"],
        )
    )

    assert ws.lookup.turn_summary("t1") == (1, 3, 0)


def test_turn_summary_caches_until_journal_or_files_change(
    tmp_path, monkeypatch
):
    """Сводка зовётся на каждом завершении хода — лишний раз файлы не читаем."""
    from gc_core.agent_edits import deltas

    calls = []
    real = deltas.delta_vs_disk

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(deltas, "delta_vs_disk", counting)

    ws = _Workspace(tmp_path)
    ws.snapshot("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    ws.journal.append(make_rec(path="a.py", turn="t1", call_id="c1"))

    assert ws.lookup.turn_summary("t1") == (1, 1, 0)
    assert ws.lookup.turn_summary("t1") == (1, 1, 0)
    assert len(calls) == 1

    ws.write("a.py", "a\nb\nc\nd\ne\n")
    assert ws.lookup.turn_summary("t1") == (1, 4, 0)
    assert len(calls) == 2

    ws.journal.append(
        make_rec(
            path="b.py",
            turn="t1",
            call_id="c2",
            ts=2.0,
            op="create",
            add=1,
            del_=0,
        )
    )
    ws.write("b.py", "x\n")
    assert ws.lookup.turn_summary("t1") == (2, 5, 0)
    assert len(calls) == 4


def test_turn_summary_counts_shell_edits_via_counting_base(tmp_path):
    """Ход, где файл правила только команда: сводка даёт итог, а не нули.

    Счётная база (`.gitorig`/`.gitturn`, содержимое индекса git) до этой
    задачи не имела ни одного потребителя — сводка звала точный снимок и
    падала на счётчики журнала.
    """
    ws = _Workspace(tmp_path)
    ws.store.capture_shell_turn("fmt.py", "a\nb\nc\n".encode("utf-8"), "t1")
    ws.write("fmt.py", "a\nB\nc\n")
    ws.journal.append(
        make_rec(
            path="fmt.py", turn="t1", call_id=None, op="shell", add=9, del_=9
        )
    )

    assert ws.lookup.turn_summary("t1") == (1, 1, 1)


def test_turn_summary_honours_config_limits(tmp_path):
    """Лимиты секции `agent_edits` доезжают до расчёта, а не остаются дефолтными."""
    ws = _Workspace(tmp_path, max_diff_source_bytes=10)
    ws.snapshot("big.py", "a\n")
    ws.write("big.py", "".join(f"line{i}\n" for i in range(50)))
    ws.journal.append(
        make_rec(path="big.py", turn="t1", call_id="c1", add=7, del_=0)
    )

    # Сравнить стороны нечем (кап) — счётчики берутся из журнала, а дефолтный
    # кап в 2 МБ дал бы полноценную дельту в 50 строк.
    assert ws.lookup.turn_summary("t1") == (1, 7, 0)


def test_turn_summary_without_launch_root_counts_files_only(tmp_path):
    """Без launch_root ни снимков, ни файлов не найти — цифр не выдумываем."""
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    journal = EditsJournal(edits_journal_path(str(threads_dir), "cli", "th1"))
    lookup = EditsLookup(str(threads_dir), "cli", "th1")
    journal.append(
        make_rec(path="a.py", turn="t1", call_id="c1", add=3, del_=1)
    )

    assert lookup.turn_summary("t1") == (1, 0, 0)


def test_last_turn_id_and_records_for_turn(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(turn="t1", call_id="c1"))
    journal.append(make_rec(turn="t2", call_id="c2"))
    journal.append(make_rec(turn="t2", call_id="c3", path="b.py"))

    assert lookup.last_turn_id() == "t2"
    recs = lookup.records_for_turn("t2")
    assert [r.call_id for r in recs] == ["c2", "c3"]


def test_missing_journal_returns_empty_without_exceptions(tmp_path):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    lookup = EditsLookup(str(threads_dir), "cli", "ghost")

    assert lookup.by_call_id("c1") is None
    assert lookup.turn_summary("t1") == (0, 0, 0)
    assert lookup.last_turn_id() is None
    assert lookup.records_for_turn("t1") == []


def test_index_updates_on_live_append(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(call_id="c1"))

    assert lookup.by_call_id("c1") is not None
    assert lookup.by_call_id("c2") is None

    journal.append(make_rec(call_id="c2"))

    assert lookup.by_call_id("c2") is not None


def test_by_call_id_survives_journal_reset_with_same_record_count(tmp_path):
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    journal_path = edits_journal_path(str(threads_dir), "cli", "th1")
    journal = EditsJournal(journal_path)
    lookup = EditsLookup(str(threads_dir), "cli", "th1")

    journal.append(make_rec(call_id="old1", ts=1.0))
    journal.append(make_rec(call_id="old2", ts=2.0))
    assert lookup.by_call_id("old1") is not None  # индекс построен

    # Сброс треда (thread_store.reset_thread_files): журнал удаляется и
    # пишется заново с нуля. Новый файл случайно совпадает по числу записей
    # со старым, но содержит другие call_id и другие метки времени.
    journal_path.unlink()
    journal2 = EditsJournal(journal_path)
    journal2.append(make_rec(call_id="new1", ts=100.0))
    journal2.append(make_rec(call_id="new2", ts=200.0))

    assert lookup.by_call_id("new1") is not None
    assert lookup.by_call_id("old1") is None


def test_by_call_id_returns_last_when_duplicated(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(call_id="c1", path="a.py", add=1, del_=0))
    journal.append(make_rec(call_id="c1", path="b.py", add=2, del_=0))

    rec = lookup.by_call_id("c1")

    assert rec.path == "b.py"


def test_turn_ids_skip_turns_without_call_id(tmp_path):
    """Ход из одних шелл-записей блоков в ленте не даёт — в окно реплея не идёт."""
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(turn="t1", call_id="c1"))
    journal.append(make_rec(turn="t2", call_id=None, op="shell"))
    journal.append(make_rec(turn="t3", call_id="c3"))

    assert lookup.turn_ids() == ["t1", "t3"]


def _shell(path, turn="t1", command="x1", **kw):
    return make_rec(
        path=path,
        turn=turn,
        call_id=None,
        op="shell",
        command_call_id=command,
        **kw,
    )


def test_by_command_lists_records_in_journal_order(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    for path in ("a.py", "b.py", "c.py"):
        journal.append(_shell(path, command="c1"))
    journal.append(_shell("d.py", command="c2"))
    journal.append(make_rec(path="e.py", call_id="c1"))

    assert [r.path for r in lookup.by_command("c1")] == [
        "a.py",
        "b.py",
        "c.py",
    ]
    assert [r.path for r in lookup.by_command("c2")] == ["d.py"]
    assert lookup.by_command("nope") == []
    assert lookup.by_command("") == []


def test_by_command_same_id_in_two_turns_is_silent(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(_shell("a.py", turn="t1", command="c1"))
    journal.append(_shell("b.py", turn="t2", command="c1"))

    assert lookup.by_command("c1") == []


def test_turn_ids_count_turns_with_command_records(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(make_rec(turn="t1", call_id="c1"))
    journal.append(_shell("a.py", turn="t2", command="x2"))
    journal.append(_shell("b.py", turn="t3", command=None))

    assert lookup.turn_ids() == ["t1", "t2"]


def test_command_paths_of(tmp_path):
    lookup, journal = make_lookup(tmp_path)
    journal.append(_shell("a.py", command="c1", command_paths=6))
    journal.append(_shell("b.py", command="c1", command_paths=6))
    journal.append(_shell("c.py", command="c2"))

    assert lookup.command_paths_of("c1") == 6
    assert lookup.command_paths_of("c2") is None
    assert lookup.command_paths_of("nope") is None


def test_lookup_for_settings_takes_limits_from_agent_edits(tmp_path):
    """Одна фабрика журнала правок для ленты и превью: лимиты диффа — из
    секции `agent_edits` настроек (`edits_view.diff_limits`)."""
    from gc_testkit.tui_support import tui_settings
    from gc_tui.services.edits_lookup import lookup_for_settings

    settings = tui_settings(
        {
            "agent_edits.hunk_context_lines": 1,
            "agent_edits.max_diff_source_bytes": 1000,
        }
    )
    lookup = lookup_for_settings(
        str(tmp_path), "cli", "th1", settings, launch_root=str(tmp_path)
    )
    assert lookup._hunk_context_lines == 1
    assert lookup._max_source_bytes == 1000
    assert lookup._launch_root == str(tmp_path)
