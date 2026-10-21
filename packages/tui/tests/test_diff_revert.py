"""Откат правок с экрана изменений: `r` (файл) и `Ctrl+R` (всё представление).

Откат пишет и удаляет файлы пользователя, поэтому проверяется не только «кнопка
сработала», а весь периметр безопасности:

* подтверждение — ПОЛНОЭКРАННОЕ и показывает прямой дифф «база → диск»: то, что
  исчезнет; отказ от подтверждения не трогает диск вовсе;
* счётная база шелл-канала (содержимое ИНДЕКСА git) на диск не попадает ни одним
  путём — такие пути откату не подлежат и уходят в пропущенные;
* во время живого прогона откат недоступен (гонка с пишущим агентом);
* после отката данные перечитываются, а агент получает technical-сообщение —
  иначе следующая правка ляжет поверх выдуманного состояния файла.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.revert import (
    REASON_NO_BASELINE,
    REASON_NOT_REVERTABLE,
    RevertOutcome,
)
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_core.utils.file_text import sha256_hex_of_bytes
from gc_testkit.tui_support import (
    as_settings,
    span_style,
    tui_settings,
    wait_until,
)
from gc_tui.palette import ERR, OK
from gc_tui.screens.diff import (
    APPROX,
    BUSY_REVERT,
    NOT_REVERTABLE,
    VIEWER_NO_PREVIEW,
    DiffFileViewer,
    DiffScreen,
    build_reset_title,
    build_revert_title,
    reset_loss_line,
    reset_target_line,
    revert_ack,
)
from gc_tui.screens.diff_revert_confirm import (
    CONFIRM_FOOTER,
    PREVIEW_NOTHING,
    RESET_CONFIRM_FOOTER,
    SKIPPED_LINE,
    WARN_EXTERNAL,
    RevertConfirmScreen,
    confirm_footer,
    file_caption,
)
from gc_tui.services.edits_view import SOURCE_SESSION, SOURCE_TURN

pytest.importorskip("textual")


# --- фикстуры данных ---------------------------------------------------------


def _sha(text: str) -> str:
    """Хэш содержимого ровно так, как его пишет рекордер, — от СЫРЫХ байтов.

    Выдуманные хэши в записи о файле, лежащем на диске, — это не «любое
    значение»: признак `извне` сверяет журнал с диском, и такая запись честно
    читается как «состояние на диске не наше».
    """
    return sha256_hex_of_bytes(text.encode("utf-8"))


def _rec(
    path="a.py",
    turn="t1",
    call_id="c1",
    op="edit",
    add=1,
    del_=1,
    ts=1.0,
    hunks="",
    before=None,
    after=None,
) -> EditRecord:
    return EditRecord(
        path=path,
        turn_id=turn,
        ts=ts,
        call_id=call_id,
        op=op,
        hunks=hunks,
        add=add,
        del_=del_,
        before_sha256=before,
        after_sha256=after,
        flags=[],
    )


class _Workspace:
    """Тред с журналом и снимками + файлы на диске под launch_root."""

    def __init__(self, tmp_path: Path) -> None:
        self.threads_dir = tmp_path / "threads"
        self.threads_dir.mkdir(parents=True)
        self.root = tmp_path / "proj"
        self.root.mkdir(parents=True)
        self.journal = EditsJournal(
            edits_journal_path(str(self.threads_dir), "cli", "th1")
        )
        self.store = SnapshotStore(
            snapshots_root(str(self.root)), "cli_th1", self.threads_dir
        )
        self.acks: list[str] = []
        self.feedback: list[tuple[list[str], list[str]]] = []

    @property
    def config(self) -> dict:
        return {
            "launch_root": str(self.root),
            "agent.model": "M",
            "agent_edits": {},
        }

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def base(self, rel: str, text: str) -> None:
        """Точный снимок «до» — только он даёт право на откат.

        Ревизия HEAD пишется рядом, ровно как это делает рекордер (§3.2):
        без неё признака «коммит затронул путь уже в этой сессии» нет, и
        ступень HEAD не появится ни у одного сценария (фикс C1). Вне git
        ревизии не существует — там аргумент просто ``None``.
        """
        from gc_core.agent_edits import git_ops

        self.store.capture_session(
            rel,
            text.encode("utf-8"),
            head_rev=git_ops.head_rev_from_files(str(self.root)),
        )

    def counting_base(self, rel: str, text: str) -> None:
        """Счётная база шелл-канала (содержимое индекса git) — откату не годится."""
        self.store.capture_shell_session(rel, text.encode("utf-8"))

    def accept(self, rel: str, text: str) -> None:
        """Принять путь «вручную» мимо экрана — готовый снимок для фикстур
        подтверждения сброса (тот же помощник, что в `test_diff_accept.py`)."""
        self.store.capture_accepted(rel, text.encode("utf-8"), head_oid=None)

    def snapshot_path(self, suffix: str) -> Path:
        return next(snapshots_root(str(self.root)).glob(f"*{suffix}"))

    def counting_base_is_older(self) -> None:
        """Опасная форма: счётная база записана РАНЬШЕ точной и побеждает в счёте."""
        now = time.time()
        os.utime(self.snapshot_path(".gitorig"), (now - 30, now - 30))
        os.utime(self.snapshot_path(".orig"), (now - 10, now - 10))

    def screen(self, **kwargs) -> DiffScreen:
        kwargs.setdefault("on_ui_ack", self.acks.append)
        kwargs.setdefault(
            "on_undo_feedback",
            lambda restored, deleted: self.feedback.append(
                (list(restored), list(deleted))
            ),
        )
        return DiffScreen(
            launch_root=str(self.root),
            settings=as_settings(self.config),
            threads_dir=str(self.threads_dir),
            system_id="cli",
            thread_id="th1",
            **kwargs,
        )


def _git_init(root: Path) -> None:
    """Минимальный `git init` — по образцу `test_edits_view.py`, только здесь
    нужен ровно одному сценарию (ступень «закоммичено»), поэтому импорты
    свои, а не в общей шапке файла.
    """
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
        ("config", "core.autocrlf", "false"),
    ):
        subprocess.run(
            ["git", *args], cwd=str(root), check=True, capture_output=True
        )


def _plain(widget) -> str:
    return str(widget.render())


def _body(screen) -> str:
    return "\n".join(_plain(w) for w in screen.query("#trace > Static"))


_DIFF_PART = "«участок диффа»"


def _tree_rows(screen) -> list[tuple[str, str]]:
    """Форма дерева подтверждения: строка тела и её классы `tree-*`.

    Участок диффа заменяется меткой — к дереву он не относится, а текста в нём
    десятки строк. Классы собираются ВСЕ: на строке дерева их обязан быть ровно
    один (отступы задаёт он своим shorthand'ом, и второй такой же класс перетёр
    бы их целиком — ловушка Textual 2), у участка диффа — ни одного.
    """
    return [
        (
            _DIFF_PART if widget.has_class("diff-hunk") else _plain(widget),
            " ".join(
                sorted(c for c in widget.classes if c.startswith("tree-"))
            ),
        )
        for widget in screen.query("#trace > Static")
    ]


async def _open(pilot, screen):
    await pilot.app.push_screen(screen)
    await pilot.pause()
    return screen


# --- чистые функции: заголовок, акк, предупреждения --------------------------


def test_title_names_file_and_what_will_be_undone():
    title = build_revert_title(
        ["src/tui/screens/diff.py"], add=84, del_=12, exact=True, width=100
    )
    assert title == "откат · src/tui/screens/diff.py · будет отменено +84 −12"


def test_title_of_many_files_counts_them():
    title = build_revert_title(
        ["a.py", "b.py", "c.py"], add=9, del_=3, exact=True, width=100
    )
    assert title == "откат · 3 файла · будет отменено +9 −3"


def test_title_marks_inexact_numbers():
    title = build_revert_title(
        ["uv.lock"], add=3, del_=1, exact=False, width=100
    )
    assert f"{APPROX} +3 −1" in title


def test_title_drops_zero_counters():
    """`countable=False` даёт нули — подавать их как «отменяется 0 строк» нельзя."""
    title = build_revert_title(
        ["logo.png"], add=0, del_=0, exact=True, width=100
    )
    assert title == "откат · logo.png"
    assert "отменено" not in title


def test_title_truncates_long_path_from_the_left():
    """Режем слева: имя файла отвечает на «что откатываем», общий корень — нет."""
    deep = "src/very/deep/nested/package/module/screens/engine.py"
    title = build_revert_title([deep], add=7, del_=2, exact=True, width=60)
    assert len(title) <= 60
    assert "engine.py" in title and "…" in title
    assert "будет отменено +7 −2" in title
    assert deep not in title

    # Ширины не хватает даже на минимум пути — числа и слово не жертвуются
    # (то же правило, что в шапке вьюера), хвост строки срежет CSS.
    tight = build_revert_title([deep], add=7, del_=2, exact=True, width=30)
    assert tight.endswith("будет отменено +7 −2")
    assert "engine.py" in tight


def test_reset_title_names_the_single_file():
    """`u` — тот же жанр, что у одиночного отката: имя файла, без счётчиков
    (сброс не пишет на диск, отменять в строках нечего, задача 4 доп.)."""
    title = build_reset_title(
        ["src/tui/screens/diff.py"], mass=False, width=100
    )
    assert title == "Сброс принятия · src/tui/screens/diff.py"


def test_reset_title_counts_files_in_genitive_after_the_mass_gesture():
    """`Ctrl+U` считает файлы ВСЕГДА, даже на одном (старый гейт `mass`,
    докстринг бывшего `reset_confirm_message`) — родительным падежом ПОСЛЕ
    ПРЕДЛОГА («у 1 файла»/«у 2 файлов», не «у 1 файл»/«у 2 файла»)."""
    assert (
        build_reset_title(["a.py"], mass=True, width=100)
        == "Сброс принятия у 1 файла"
    )
    assert (
        build_reset_title(["a.py", "b.py"], mass=True, width=100)
        == "Сброс принятия у 2 файлов"
    )


def test_ack_names_the_single_file_and_what_happened_to_it():
    assert revert_ack(RevertOutcome(restored=["src/a.py"])) == (
        "откат · восстановлено: src/a.py"
    )
    assert (
        revert_ack(RevertOutcome(deleted=["new.py"]))
        == "откат · удалено: new.py"
    )


def test_ack_counts_files_when_there_are_many():
    outcome = RevertOutcome(
        restored=["a.py", "b.py"], deleted=["c.py", "d.py", "e.py"]
    )
    assert (
        revert_ack(outcome)
        == "откат · восстановлено: 2 файла · удалено: 3 файла"
    )


def test_ack_explains_every_skipped_reason():
    outcome = RevertOutcome(
        restored=["a.py"],
        skipped=[
            ("uv.lock", REASON_NOT_REVERTABLE),
            ("b.py", REASON_NO_BASELINE),
        ],
    )
    ack = revert_ack(outcome)
    assert ack.startswith("откат · восстановлено: a.py · ")
    assert f"пропущено: uv.lock — {NOT_REVERTABLE}" in ack
    assert "пропущено: b.py — " in ack
    # причины разные — и текст у них разный, иначе пользователь их не различит
    assert ack.count(NOT_REVERTABLE) == 1


def test_ack_of_fully_skipped_revert_says_nothing_was_done():
    outcome = RevertOutcome(skipped=[("uv.lock", REASON_NOT_REVERTABLE)])
    ack = revert_ack(outcome)
    assert ack.startswith("откат не выполнен · ")
    assert NOT_REVERTABLE in ack


def test_every_skip_reason_has_a_human_text():
    """Машинный код в акке — это `not_revertable` на экране пользователя."""
    from gc_core.agent_edits import revert as revert_module

    codes = {
        value
        for name, value in vars(revert_module).items()
        if name.startswith("REASON_")
    }
    assert len(codes) >= 5
    for code in codes:
        ack = revert_ack(RevertOutcome(skipped=[("a.py", code)]))
        assert code not in ack, code


def test_nothing_is_left_to_truncate_the_preview_with():
    """Р22: набор не режется — ни хвостов, ни бюджета строк, ни капа файлов.

    Проверяется API, а не только вид: уцелевшая функция-хвост или параметр
    бюджета — это готовая обрезка, которую вернут первым же «а давайте
    показывать не всё».

    Компактный вид больших наборов (`FULL_PREVIEW_MAX_FILES`, решение
    владельца 2026-09-24) — не обрезка: каждый файл в нём назван поимённо, со
    счётчиками, целью и предупреждениями; опускаются только тела диффов, и
    это сказано строкой.
    """
    import inspect

    import gc_tui.screens.diff as list_mod
    import gc_tui.screens.diff_revert_confirm as mod

    for dead in ("preview_tail", "files_tail"):
        assert not hasattr(mod, dead), dead
    for dead in ("PREVIEW_MAX_LINES", "PREVIEW_MAX_FILES"):
        assert not hasattr(list_mod, dead), dead
    params = inspect.signature(RevertConfirmScreen.__init__).parameters
    assert "hidden_files" not in params and "max_preview_lines" not in params


def test_confirm_body_is_cut_into_even_chunks_not_into_edits():
    """Тело подтверждения режется ровными кусками, а не участками по правкам.

    Курсора по правкам здесь нет, выравнивать участки не на что, а участок на
    каждую мелкую правку — это сотни виджетов на одном файле: монтаж выходит
    дороже самого рендера (замер Ревизии 12). Терять при этом не должно ни
    строки: склейка кусков обязана дать ровно то же тело.
    """
    from math import ceil

    from gc_tui.screens.diff import CONTEXT_CHUNK, split_body
    from gc_tui.services.diff_render import render_hunks

    hunks = "".join(
        f"@@ -{i * 10 + 1},4 +{i * 10 + 1},4 @@\n ctx {i}a\n-old {i}\n+new {i}\n ctx {i}b\n"
        for i in range(30)
    )
    whole = render_hunks(hunks, path="a.py", fill_width=None).text.plain
    even = split_body(hunks, path="a.py", fill_width=None, by_edits=False)
    by_edits = split_body(hunks, path="a.py", fill_width=None)

    assert len(even) == ceil(len(whole.splitlines()) / CONTEXT_CHUNK)
    assert len(by_edits) > 10 * len(even)  # ради этого куски и ровные
    assert all(part.edit is None for part in even)
    assert "\n".join(part.text.plain for part in even) == whole


def test_file_caption_carries_the_counters_of_the_file():
    """Строка имени в дереве — вклад файла в итог шапки (Р22), без обрамления.

    Тире по краям ушли вместе с курсором (Е3): блок опознаётся отступом
    вложенных строк, а рамка из тире делала из документа таблицу.
    """
    assert file_caption("calc.py", 185, 0) == "calc.py · +185 −0"
    # Несчётный путь: нулей нет — по тому же правилу, что в шапке экрана.
    assert file_caption("logo.png", 0, 0, countable=False) == "logo.png"


def test_confirm_footer_carries_the_count_in_dialog_form():
    """Е3: объём набора стоит на самом действии, как надпись кнопки диалога.

    Курсора по файлам больше нет, и «Enter» отвечает за ВЕСЬ набор — число
    в футере снимает вопрос «а что именно подтвердит Enter». Форма
    числительная (`files_word`): «1 файл», «2 файла», «5 файлов».
    """
    assert confirm_footer(count=2, can_scroll=False) == (
        "Enter откатить 2 файла · Esc отмена"
    )
    assert confirm_footer(count=1, can_scroll=True) == (
        "Enter откатить 1 файл · ↑↓ PgUp PgDn · Esc отмена"
    )
    assert confirm_footer(count=5, can_scroll=False, reset=True) == (
        "Enter сбросить 5 файлов · Esc отмена"
    )
    # `n`/`p` ушли из клавиш — и из полосы вместе с ними.
    assert "n след" not in confirm_footer(count=3)
    # Канон словаря для стражей — тот же сбор, а не вторая его редакция.
    assert (
        CONFIRM_FOOTER == "Enter откатить 1 файл · ↑↓ PgUp PgDn · Esc отмена"
    )
    assert (
        RESET_CONFIRM_FOOTER
        == "Enter сбросить 1 файл · ↑↓ PgUp PgDn · Esc отмена"
    )


def test_confirm_footer_drops_scrolling_when_the_body_fits_the_frame():
    """Мёртвые клавиши не рекламируются (Д5 второго смоука): тело поместилось
    в кадр целиком, двигать его некуда, и `↑↓ PgUp PgDn` уходит из полосы.

    Слово действия при этом не трогается ни в одной из комбинаций: сброс
    остаётся сбросом, откат — откатом.
    """
    assert confirm_footer(count=1, can_scroll=False) == (
        "Enter откатить 1 файл · Esc отмена"
    )
    assert confirm_footer(count=3, can_scroll=False, reset=True) == (
        "Enter сбросить 3 файла · Esc отмена"
    )
    assert confirm_footer(count=3, can_scroll=True, reset=True) == (
        "Enter сбросить 3 файла · ↑↓ PgUp PgDn · Esc отмена"
    )


def test_external_warning_speaks_about_edits_past_the_agent():
    """Текст говорит о факте: после правки агента файл менялся мимо него.

    Годится обоим наблюдениям признака — и разрыву before-цепочки в журнале, и
    чужому содержимому на диске.
    """
    assert WARN_EXTERNAL == (
        "После правки агента файл менялся извне — откат отменит и эти изменения."
    )


def test_revert_texts_keep_the_screen_vocabulary():
    """Р1 и словарь гейта: ни approve/pending, ни «одобрить»/«ожидает принятия».

    Голое `приня*` («принять»/«принятие») ушло из бан-листа задачей 4 доп.:
    экран легально стал ДВУСМЫСЛЕННЫМ — сюда переехало и подтверждение
    сброса принятия (`RevertConfirmScreen.for_reset`, С7), а «принятие» — не
    синоним забракованного «approve», а разрешённое слово словаря (Global
    Constraints: «принять/принято/принят» — разрешены). Но грубый бан по
    подстроке заодно ловил и «одобрить»/«ожидает принятия» («приня» — часть
    «ПРИНЯтия») — те остаются запрещены безусловно (Global Constraints), и
    ИХ снятие с бана было бы дырой, а не последствием разрешения «приня».
    Список — тот же, что и в параллельном страже той же волны
    (`test_diff_screen.py::test_no_approve_wording_anywhere`), решающем то
    же напряжение для соседнего файла: снял голое «приня», явно оставил
    «одобрить»/«ожидает принятия» поимённо.
    """
    import inspect

    import gc_tui.screens.diff_revert_confirm as mod
    from gc_tui.screens.diff import (
        revert_target_arrow,
        revert_target_line,
        revert_target_warnings,
    )
    from gc_tui.services.edits_view import RevertTarget

    source = inspect.getsource(mod).lower()
    for dead in ("approve", "pending", "одобрить", "ожидает принятия"):
        assert dead not in source, dead

    target = RevertTarget(
        "a.py",
        "turn",
        "начало хода",
        None,
        ("заденет коммит e5f2c1a, сделанный после начала хода",),
    )
    texts = [
        WARN_EXTERNAL,
        PREVIEW_NOTHING,
        CONFIRM_FOOTER,
        BUSY_REVERT,
        NOT_REVERTABLE,
        confirm_footer(count=2, reset=True),
        file_caption("a.py", 1, 0),
        SKIPPED_LINE,
        build_revert_title(["a.py"], add=1, del_=1, exact=True, width=80),
        revert_ack(
            RevertOutcome(
                restored=["a.py"], skipped=[("b.py", REASON_NOT_REVERTABLE)]
            )
        ),
        revert_target_line(
            RevertTarget(
                "a.py", "accepted", "принятое состояние (14:32)", None, ()
            )
        ),
        revert_target_arrow(target),
        *revert_target_warnings(target),
    ]
    for text in texts:
        low = text.lower()
        assert "хэнк" not in low and "дифф" not in low, text
        assert "undo" not in low and "approve" not in low, text


# --- подтверждение: каркас и тело --------------------------------------------


@pytest.mark.asyncio
async def test_r_shows_fullscreen_confirm_not_modal(tmp_path):
    """Модалка шириной 56 дифф не вмещает — подтверждение полноэкранное."""
    from gc_tui.app import TuiApp
    from textual.screen import ModalScreen

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\nb\n")
    ws.write("a.py", "a\nB\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()

        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        assert not isinstance(confirm, ModalScreen)
        # каркас read-only экрана (§16): шапка, тело, футер
        assert confirm.query("#header-left") and confirm.query("#trace")
        # Дифф в две строки помещается в кадр целиком — прокрутка мертва, и
        # полоса о ней молчит (Д5); действие несёт объём набора (Е3).
        assert _plain(confirm.query_one("#footer")) == confirm_footer(
            count=1, can_scroll=False
        )
        header = _plain(confirm.query_one("#header-left"))
        assert header.startswith("откат · a.py")
        assert "будет отменено" in header
        # счётчики отменяемого — того же цвета, что в списке и вьюере
        painted = confirm.query_one("#header-left").content
        assert span_style(painted, "+1") == OK
        assert span_style(painted, "−1") == ERR
        # Р16: тело в рамке на всю высоту между шапкой и футером
        lines = [
            "".join(seg.text for seg in strip).rstrip()
            for strip in pilot.app.screen._compositor.render_strips()
        ]
        top = next(i for i, line in enumerate(lines) if line.startswith("╭"))
        bottom = next(
            i for i, line in enumerate(lines) if line.startswith("╰")
        )
        assert top == 2, lines
        footer_h = confirm.query_one("#footer").outer_size.height
        assert bottom == len(lines) - footer_h - 1, lines
        assert any("+B" in line and line.startswith("│") for line in lines), (
            lines
        )


@pytest.mark.asyncio
async def test_confirm_shows_direct_diff_of_what_disappears(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "старое\n")
    ws.write("a.py", "новое\n")
    ws.journal.append(_rec("a.py", hunks="@@ -1 +1 @@\n-мусор\n+мусор\n"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        body = _body(pilot.app.screen)
        assert "+новое" in body and "-старое" in body
        assert "мусор" not in body  # тело не из журнала, а «база → диск»


@pytest.mark.asyncio
async def test_confirm_enter_restores_file_content(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "исходное\n")
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert ws.read("a.py") == "исходное\n"
        assert pilot.app.screen is scr  # вернулись к списку
        assert ws.acks == ["откат · восстановлено: a.py"]


@pytest.mark.asyncio
async def test_confirm_escape_leaves_file_untouched(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "исходное\n")
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert ws.read("a.py") == "правленое\n"
        assert pilot.app.screen is scr
        assert ws.acks == [] and ws.feedback == []


@pytest.mark.asyncio
async def test_created_file_revert_deletes_it_and_says_so(tmp_path):
    """У созданного файла базы нет и быть не может — откат его удаляет."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("new.py", "сочинено\n")
    ws.journal.append(_rec("new.py", op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert not (ws.root / "new.py").exists()
        assert ws.acks == ["откат · удалено: new.py"]
        assert ws.feedback == [([], ["new.py"])]


@pytest.mark.asyncio
async def test_external_edit_shows_warning(tmp_path):
    """Жёлтое предупреждение — только там, где файл правился извне."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\n")
    # Разрыв before-цепочки: между нашими правками файл трогал кто-то ещё.
    # Последняя запись при этом говорит правду о диске — иначе предупреждение
    # поднялось бы вторым наблюдением (чужое содержимое на диске), и проверка
    # шла бы мимо цели.
    ws.journal.append(_rec("a.py", ts=1.0, before="aa", after="bb"))
    ws.journal.append(
        _rec("a.py", ts=2.0, before="ЧУЖОЕ", after=_sha("a\nb\n"))
    )
    ws.base("clean.py", "x\n")
    ws.write("clean.py", "x\ny\n")
    ws.journal.append(
        _rec("clean.py", ts=3.0, before=_sha("x\n"), after=_sha("x\ny\n"))
    )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert [e.path for e in scr._entries] == ["clean.py", "a.py"]

        await pilot.press("r")  # чистый файл — без предупреждения
        await pilot.pause()
        assert WARN_EXTERNAL not in _body(pilot.app.screen)
        await pilot.press("escape")
        await pilot.pause()

        await pilot.press("down")
        await pilot.press("r")
        await pilot.pause()
        assert WARN_EXTERNAL in _body(pilot.app.screen)


@pytest.mark.asyncio
async def test_shell_only_file_is_offered_as_skipped_not_reverted(tmp_path):
    """Счётная база — содержимое ИНДЕКСА git; на диск она не попадает никогда."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.counting_base("shell.py", "ИНДЕКС\n")
    ws.write("shell.py", "рабочая копия\n")
    ws.journal.append(_rec("shell.py", call_id=None, op="shell"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        entry = scr._entries[0]
        assert entry.revertable is False and entry.countable is True

        await pilot.press("r")
        await pilot.pause()
        # подтверждения нет вовсе: откатывать нечем, а пустой диалог — враньё
        assert pilot.app.screen is scr
        assert ws.read("shell.py") == "рабочая копия\n"
        assert ws.acks == [] and ws.feedback == []


@pytest.mark.asyncio
async def test_huge_diff_is_shown_whole_and_cut_into_parts(tmp_path):
    """Р22 + Р23: длинный дифф весь на месте, но собирается по мере прокрутки.

    «Показать целиком» и «собрать целиком» — разные вещи. Место под КАЖДЫЙ
    участок зарезервировано с первого кадра: сумма высот равна числу строк
    диффа, и ровно её показывает полоса прокрутки — обрезки нет. А картинку
    получают участки, попавшие в кадр; до дальних доезжает прокрутка, и хвост
    диффа обязан по ней показаться — иначе «показываем всё» было бы обещанием
    без покрытия.
    """
    from math import ceil

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import CONTEXT_CHUNK
    from gc_tui.services.diff_render import _parse, _take

    ws = _Workspace(tmp_path)
    ws.base("big.py", "".join(f"line{n:04d}\n" for n in range(600)))
    ws.write(
        "big.py",
        "".join(
            f"{'LINE' if n % 20 == 0 else 'line'}{n:04d}\n" for n in range(600)
        ),
    )
    ws.journal.append(_rec("big.py", add=30, del_=30))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(100, 24)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        blocks = [
            w
            for w in confirm.query("#trace > Static")
            if w.has_class("diff-hunk")
        ]
        _path, delta = confirm._preview[0]
        # Строки тела без потолка — второе мнение, независимое от `HunkBody`.
        parsed = _parse(delta.hunks or "")
        rows = len(_take(parsed, sum(1 for r in parsed if r.kind != "sep")))
        assert rows > 200  # прежний потолок остался позади
        # Место под тело — всё, до последней строки: полоса прокрутки честна.
        assert sum(int(w.styles.height.value) for w in blocks) == rows
        # Задача 13: над телом стоит строка цели («вернёт к началу сессии») —
        # служебная (текст + отступ `.line-meta`), а не участок диффа; полная
        # высота полотна обязана включать и место под неё же. Место перед
        # первым блоком диффа — это ровно она: в этом наборе (один файл, без
        # предупреждений) больше ничего над телом не стоит.
        assert any(
            "вернёт к" in _plain(w) for w in confirm.query("#trace > Static")
        )
        assert confirm.query_one("#trace").virtual_size.height == (
            rows + blocks[0].virtual_region.y
        )
        # Тело нарезано, и куски РОВНЫЕ: участок на каждую из тридцати правок
        # дал бы под сотню виджетов вместо шести.
        assert len(blocks) == ceil(rows / CONTEXT_CHUNK) > 1
        # Собрано не всё — только окно вокруг кадра (Р23).
        built = [w for w in blocks if _plain(w)]
        assert 0 < len(built) < len(blocks)
        body = _body(confirm)
        assert "… и ещё" not in body
        assert "+LINE0000" in body  # начало собрано сразу

        # ... а хвост подъезжает прокруткой (она плавная — ждём наблюдаемое).
        await pilot.press("end")
        await wait_until(
            pilot,
            lambda: "+LINE0580" in _body(confirm),
            "последняя строка превью собралась",
        )


@pytest.mark.asyncio
async def test_preview_fill_reaches_right_edge_minus_scrollbar(tmp_path):
    """Заливка тянется до края тела и после смены ширины — как во вьюере.

    Ширина на `Resize` берётся ИЗ СОБЫТИЯ: экран получает его раньше, чем
    `#trace` примет новый размер, и замер контейнера отдал бы старую ширину.
    Отступ тела слева вычитается вместе со скроллбаром — иначе заливка вылезла
    бы под него ровно на ширину отступа.
    """
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import SCROLLBAR_COLUMN
    from gc_tui.screens.diff_revert_confirm import BODY_INDENT

    ws = _Workspace(tmp_path)
    ws.base("a.py", "".join(f"line{n:02d}\n" for n in range(12)))
    ws.write("a.py", "".join(f"LINE{n:02d}\n" for n in range(12)))
    ws.journal.append(_rec("a.py", add=12, del_=12))

    def _filled(confirm) -> set[int]:
        blocks = [
            w
            for w in confirm.query("#trace > Static")
            if w.has_class("diff-hunk")
        ]
        return {
            len(line)
            for block in blocks
            for line in _plain(block).splitlines()
            if line.lstrip().split(" ", 1)[-1].startswith(("+", "-"))
        }

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(70, 20)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        body = confirm.query_one("#trace")
        room = body.size.width - SCROLLBAR_COLUMN - BODY_INDENT
        assert _filled(confirm) == {room}

        await pilot.resize_terminal(90, 20)
        await pilot.pause()
        assert _filled(confirm) == {
            body.size.width - SCROLLBAR_COLUMN - BODY_INDENT
        }


@pytest.mark.asyncio
async def test_a_long_file_does_not_eat_the_files_behind_it(tmp_path):
    """Общий бюджет строк съедал файлы за длинным диффом — теперь их видно.

    Прежде первый же большой файл выбирал весь бюджет превью, а остальные
    уходили в хвост «… и ещё N файлов» — то есть подтверждение молчало ровно о
    том, что откат тоже сотрёт. Строки имён всех файлов стоят в полотне сразу,
    а их превью подъезжает прокруткой (Р23; прыжок `n`/`p` ушёл вместе с
    курсором — Е3).
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "".join(f"line{n:04d}\n" for n in range(500)))
    ws.write("a.py", "".join(f"LINE{n:04d}\n" for n in range(500)))
    ws.journal.append(_rec("a.py", add=500, del_=500, ts=3.0))
    for rel in ("b.py", "c.py"):
        ws.base(rel, f"до-{rel}\n")
        ws.write(rel, f"после-{rel}\n")
        ws.journal.append(_rec(rel, ts=2.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+r")
        await pilot.pause()
        confirm = pilot.app.screen
        body = _body(confirm)

        assert "a.py · +500 −500" in body
        assert "b.py · +1 −1" in body and "c.py · +1 −1" in body
        assert "… и ещё" not in body

        # Хвост набора — за тысячу строк диффа; до него доезжает прокрутка.
        await pilot.press("end")
        await wait_until(
            pilot,
            lambda: "+после-c.py" in _body(confirm),
            "последний файл набора собрался",
        )
        assert "+после-b.py" in _body(confirm)


@pytest.mark.asyncio
async def test_every_file_of_the_set_is_shown_with_its_own_counters(tmp_path):
    """Все файлы набора и их счётчики: вклад каждого в итог шапки (Р22).

    Счётчики заголовков обязаны складываться в число шапки — иначе «будет
    отменено +N» и полотно под ним говорили бы о разном.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(12):
        rel = f"f{idx:02d}.py"
        ws.base(rel, f"до{idx}\n")
        ws.write(rel, "".join(f"после{idx}-{n}\n" for n in range(idx + 1)))
        ws.journal.append(_rec(rel, ts=float(idx)))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+r")
        await pilot.pause()
        confirm = pilot.app.screen
        body = _body(confirm)

        heads = list(confirm.query("#trace > .tree-head"))
        assert len(heads) == 12  # строки имён всех файлов
        for idx in range(12):
            assert f"f{idx:02d}.py · +{idx + 1} −1" in body
            assert f"+после{idx}-{idx}" in body  # и последняя строка файла
        header = _plain(confirm.query_one("#header-left"))
        assert f"будет отменено +{sum(range(1, 13))} −12" in header

        # Счётчики строки имени красятся тем же смыслом, что и всюду на экране.
        assert span_style(heads[0].content, "+1") == OK
        assert span_style(heads[0].content, "−1") == ERR


@pytest.mark.asyncio
async def test_revert_confirm_has_no_file_cursor_and_ignores_n_p(tmp_path):
    """Е3: подтверждение — документ, а не список с выбором.

    Курсор по файлам обещал «Enter над выделенным», хотя `Enter` всегда
    подтверждал ВЕСЬ набор; вместо обещания объём стоит на самом действии
    (`Enter откатить 3 файла` — по числу превью), а `n`/`p` ушли: кадр от них
    не двигается, экран не закрывается, полоса их не называет.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(3):
        rel = f"f{idx}.py"
        ws.base(rel, "".join(f"до-{idx}-{n}\n" for n in range(40)))
        ws.write(rel, "".join(f"после-{idx}-{n}\n" for n in range(40)))
        ws.journal.append(_rec(rel, ts=float(idx), add=40, del_=40))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(100, 24)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+r")
        await pilot.pause()
        confirm = pilot.app.screen
        trace = confirm.query_one("#trace")

        assert len(confirm.query("#trace > .tree-head")) == 3
        assert confirm.query("#trace > .cursor").nodes == []
        assert trace.scroll_y == 0

        await pilot.press("n")
        await pilot.pause()
        assert confirm.query("#trace > .cursor").nodes == []
        assert trace.scroll_y == 0  # прыжка по файлам нет
        assert pilot.app.screen is confirm  # и «нет» тут не живёт

        await pilot.press("p")
        await pilot.pause()
        assert trace.scroll_y == 0
        assert pilot.app.screen is confirm

        assert _plain(confirm.query_one("#footer")) == confirm_footer(count=3)

        # Строка имени и тело под ней стоят на одной вертикали: отступ дерева
        # (`tree-head`) набирает те же две колонки, что кромка и паддинг у
        # участка диффа, — снятая кромка курсора место не сдвинула.
        block = next(
            w
            for w in confirm.query("#trace > Static")
            if w.has_class("diff-hunk")
        )
        assert (
            confirm.query_one("#trace > .tree-head").content_region.x
            == block.content_region.x
        )

        # Прокрутка на месте — она и есть единственный способ дойти до хвоста.
        await pilot.press("down")
        await wait_until(
            pilot, lambda: trace.scroll_y > 0, "тело прокрутилось"
        )


@pytest.mark.asyncio
async def test_single_file_confirm_keeps_the_name_only_in_the_title(tmp_path):
    """Один файл — строки имени в полотне нет: она уже стоит в шапке.

    Блок такого набора начинается сразу с вложенных строк (Е3): курсора нет,
    опора блоку не нужна, и подпись под шапкой была бы вторым тем же именем.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("solo.py", "до\n")
    ws.write("solo.py", "после\n")
    ws.journal.append(_rec("solo.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        assert confirm.query("#trace > .tree-head").nodes == []
        assert "solo.py" not in _body(confirm)
        assert _plain(confirm.query_one("#footer")) == confirm_footer(
            count=1, can_scroll=False
        )
        assert "n след. файл" not in CONFIRM_FOOTER

        # Клавиша ничего не ломает и подтверждение не снимает: `n` мёртвая, а
        # «нет» — это Esc.
        await pilot.press("n")
        await pilot.pause()
        assert pilot.app.screen is confirm
        assert ws.read("solo.py") == "после\n"


@pytest.mark.asyncio
async def test_revert_tree_lists_skipped_files_as_blocks(tmp_path):
    """Е3: путь без достоверной базы — свой блок дерева, а не сводная строка.

    Сводное «Пропущено: N файлов — …» стояло НАД телом и не называло никого
    поимённо: пользователь видел число, но не файл. Блок называет и файл, и
    причину — там же, где стоят остальные файлы набора. В счёт шапки и
    действия он не входит: откатывать его нечем.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("readme.md", "keep\ndrop\n")
    ws.write("readme.md", "keep\n" + "".join(f"новое{n}\n" for n in range(5)))
    ws.journal.append(_rec("readme.md", ts=2.0, add=5, del_=1))
    # счётная база шелл-канала откату не годится — путь пропускается
    ws.counting_base("app.py", "ИНДЕКС\n")
    ws.write("app.py", "рабочая копия\n")
    ws.journal.append(_rec("app.py", ts=1.0, call_id=None, op="shell"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert [(e.path, e.revertable) for e in scr._entries] == [
            ("readme.md", True),
            ("app.py", False),
        ]

        await pilot.press("ctrl+r")
        await pilot.pause()
        confirm = pilot.app.screen

        # Шапка и действие считают только откатываемых.
        assert _plain(confirm.query_one("#header-left")) == (
            "откат · readme.md · будет отменено +5 −1"
        )
        assert _plain(confirm.query_one("#footer")) == confirm_footer(
            count=1, can_scroll=False
        )
        # Пропущенный назван блоком: имя — голова, причина — вложенная строка.
        assert f"пропущен: {NOT_REVERTABLE}" == SKIPPED_LINE
        assert _tree_rows(confirm) == [
            ("readme.md · +5 −1", "tree-head"),
            ("→ начало сессии", "tree-line"),
            (_DIFF_PART, ""),
            ("", "tree-gap"),
            ("app.py", "tree-head"),
            (SKIPPED_LINE, "tree-line"),
        ]
        # Сводной строки «Пропущено: N файлов» на экране не осталось нигде.
        assert "Пропущено" not in _body(confirm)


@pytest.mark.asyncio
async def test_revert_external_warning_lives_inside_its_block(tmp_path):
    """Предупреждение «менялся извне» — строка СВОЕГО блока (Е3).

    Общей плашкой над телом оно молчало о том, к кому относится: в наборе из
    двух файлов извне правился один. Заодно проверяется форма дерева массового
    отката: между блоками ровно одна пустая строка (`tree-gap`), а внутри
    блока зазоров нет вовсе — строки несут классы дерева, гасящие `margin-top`
    у `.line-meta`, и имя файла стоит при СВОИХ строках, а не при чужих.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("clean.py", "x\n")
    ws.write("clean.py", "x\ny\n")
    ws.journal.append(_rec("clean.py", ts=3.0))
    ws.base("touched.py", "a\n")
    ws.write("touched.py", "a\nb\n")
    # разрыв before-цепочки: между правками агента файл трогал кто-то ещё
    ws.journal.append(_rec("touched.py", ts=1.0, before="aa", after="bb"))
    ws.journal.append(
        _rec("touched.py", ts=2.0, call_id="c2", before="ЧУЖОЕ", after="cc")
    )

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+r")
        await pilot.pause()
        confirm = pilot.app.screen

        assert _tree_rows(confirm) == [
            ("clean.py · +1 −0", "tree-head"),
            ("→ начало сессии", "tree-line"),
            (_DIFF_PART, ""),
            ("", "tree-gap"),
            ("touched.py · +1 −0", "tree-head"),
            ("→ начало сессии", "tree-line"),
            (WARN_EXTERNAL, "tree-warn"),
            (_DIFF_PART, ""),
        ]
        # Единственный зазор в теле — сама пустая строка между блоками: все
        # строки стоят вплотную, отступа сверху не носит ни одна.
        placed = [
            (w.region.y, w.region.height)
            for w in confirm.query("#trace > Static")
        ]
        assert all(
            y + height == nxt
            for (y, height), (nxt, _) in zip(placed, placed[1:])
        ), placed


@pytest.mark.asyncio
async def test_single_revert_has_no_name_row_and_names_target_first(tmp_path):
    """Одиночный `r`: блок без строки имени — оно уже стоит в шапке (Е3).

    Тело начинается с цели отката, и она — вложенная строка дерева, как у
    сброса: тот же жанр показа, те же отступы.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("solo.py", "до\n")
    ws.write("solo.py", "после\n")
    ws.journal.append(_rec("solo.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen

        assert _tree_rows(confirm) == [
            ("вернёт к началу сессии", "tree-line"),
            (_DIFF_PART, ""),
        ]
        assert "solo.py" not in _body(confirm)


@pytest.mark.asyncio
async def test_ctrl_r_reverts_whole_source_and_lists_skipped(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a-до\n")
    ws.write("a.py", "a-после\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.write("new.py", "сочинено\n")
    ws.journal.append(_rec("new.py", ts=2.0, op="create", add=1, del_=0))
    ws.counting_base("shell.py", "ИНДЕКС\n")
    ws.write("shell.py", "рабочая копия\n")
    ws.journal.append(_rec("shell.py", ts=3.0, call_id=None, op="shell"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("ctrl+r")
        await pilot.pause()

        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        header = _plain(confirm.query_one("#header-left"))
        assert header.startswith("откат · 2 файла")  # шелловый не в счёт
        body = _body(confirm)
        # Пропущенный — свой блок: имя и под ним причина, и назван он один раз.
        assert f"shell.py\n{SKIPPED_LINE}" in body
        assert body.count("shell.py") == 1

        await pilot.press("enter")
        await pilot.pause()
        assert ws.read("a.py") == "a-до\n"
        assert not (ws.root / "new.py").exists()
        assert (
            ws.read("shell.py") == "рабочая копия\n"
        )  # счётная база на диск не поехала
        ack = ws.acks[0]
        assert "восстановлено: a.py" in ack and "удалено: new.py" in ack
        assert f"пропущено: shell.py — {NOT_REVERTABLE}" in ack
        assert ws.feedback == [(["a.py"], ["new.py"])]
        assert pilot.app.screen is scr


@pytest.mark.asyncio
async def test_uncountable_file_says_preview_is_unavailable(tmp_path):
    """Бинарь откатывается штатно — право даёт снимок, а не наличие диффа."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("logo.png", "\x00\x01ЗАГЛУШКА")
    (ws.root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    ws.journal.append(_rec("logo.png", op="binary", add=0, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert scr._entries[0].countable is False
        assert scr._entries[0].revertable is True

        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        assert VIEWER_NO_PREVIEW in _body(confirm)
        header = _plain(confirm.query_one("#header-left"))
        assert header == "откат · logo.png"  # нулей в заголовке нет

        await pilot.press("enter")
        await pilot.pause()
        assert (
            ws.root / "logo.png"
        ).read_bytes() == "\x00\x01ЗАГЛУШКА".encode()


@pytest.mark.asyncio
async def test_preview_is_built_from_the_baseline_the_revert_writes(tmp_path):
    """Подтверждение обязано считать от ТОЧНОЙ базы, а не от счётной.

    Опасная форма: команда удалила строку `B`, инструмент вернул её обратно.
    Счётная база (`.gitorig` — содержимое индекса, записан РАНЬШЕ) совпадает с
    диском, и от неё подтверждение сказало бы «отменять нечего» — а откат
    пишет точный снимок и строку `B` уничтожает. Экран, чья единственная
    работа — показать «вот что исчезнет», обязан показать именно её.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "A\nB\nC\n")
    # Точная база — первой: поверх счётной API её больше не пишет. Пара «счётная
    # старше, точная моложе» — наследие хранилищ до этого правила; времена ниже явные.
    ws.base("a.py", "A\nC\n")  # точный снимок: перед правкой тула
    ws.counting_base("a.py", "A\nB\nC\n")  # индекс до команды
    ws.counting_base_is_older()
    ws.journal.append(
        _rec("a.py", call_id=None, op="shell", ts=1.0, add=0, del_=1)
    )
    ws.journal.append(_rec("a.py", ts=2.0, add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        # счётчики списка идут от счётной базы и показывают ноль — это их право
        assert (scr._entries[0].add, scr._entries[0].del_) == (0, 0)

        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        body = _body(confirm)
        assert PREVIEW_NOTHING not in body
        assert "+B" in body  # вот что исчезнет
        header = _plain(confirm.query_one("#header-left"))
        assert header == "откат · a.py · будет отменено +1 −0"

        await pilot.press("enter")
        await pilot.pause()
        assert ws.read("a.py") == "A\nC\n"  # ровно то, что показали


@pytest.mark.asyncio
async def test_uncountable_path_promises_no_numbers(tmp_path):
    """Счётчики журнала у несчётного пути — не обещание отката (Ф3).

    Дельту сравнить нечем (на диске бинарь), но записи журнала знают про
    текстовую правку: `+1 −1` над телом «превью недоступно» обещали бы числа,
    которых никто не считал.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.txt", "текст\n")
    (ws.root / "a.txt").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    ws.journal.append(_rec("a.txt", add=1, del_=1))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        entry = scr._entries[0]
        assert entry.countable is False
        assert (entry.add, entry.del_) == (1, 1)  # счётчики журнала, не дельта

        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        assert _plain(confirm.query_one("#header-left")) == "откат · a.txt"
        assert VIEWER_NO_PREVIEW in _body(confirm)


@pytest.mark.asyncio
async def test_confirm_header_survives_resize(tmp_path):
    """Шапка пересобирается на смену ширины: счётчики не жертвуются никогда."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    deep = "src/very/deep/nested/package/module/screens/engine.py"
    ws.base(deep, "a\n")
    ws.write(deep, "a\nb\n")
    ws.journal.append(_rec(deep, add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(120, 24)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        head = confirm.query_one("#header-left")
        assert deep in _plain(head)

        await pilot.resize_terminal(50, 24)
        await pilot.pause()
        assert head.size.height == 1  # полоса шапки — одна строка
        text = _plain(head)
        assert "будет отменено +1 −0" in text
        assert "engine.py" in text and "…" in text


@pytest.mark.asyncio
async def test_preview_says_nothing_to_undo_when_only_the_counting_base_differs(
    tmp_path,
):
    """Строка видна по СЧЁТНОЙ базе, а откату писать нечего — точная совпала.

    Прежний сценарий этой плашки (файл вернули руками к базе начала сессии) с
    экрана ушёл вовсе: строки без правки на экране изменений больше нет. Но
    плашка нужна и теперь — при разошедшихся базах строка честно видна со
    своими числами, а `r` по ней действительно ничего не изменит.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    # Точная база — первой: поверх счётной API её больше не пишет. Пара «счётная
    # старше, точная моложе» — наследие хранилищ до этого правила; времена ниже явные.
    ws.base("a.py", "правленое\n")  # а откат вернёт то же самое
    ws.counting_base("a.py", "исходное\n")  # по ней строка видна
    ws.counting_base_is_older()
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        assert PREVIEW_NOTHING in _body(pilot.app.screen)


# --- цель отката и предупреждения хода (задача 13) ----------------------------


@pytest.mark.asyncio
async def test_preview_names_target_per_step_kind(tmp_path):
    """Шапка одиночного превью называет цель — дательный падеж по ступени
    лестницы (§4.4/§13); факты (время, ref коммита) берутся готовыми из
    `RevertTarget.label`, а не пересчитываются заново.
    """
    from gc_tui.app import TuiApp
    from gc_tui.palette import ACCENT
    from gc_tui.screens.diff import revert_target_line
    from gc_tui.services.edits_view import EditsView, RevertTarget

    accepted = RevertTarget(
        "a.py", "accepted", "принятое состояние (14:32)", None, ()
    )
    assert (
        revert_target_line(accepted) == "вернёт к принятому состоянию (14:32)"
    )

    head_with_ref = RevertTarget(
        "a.py",
        "head",
        "закоммиченное состояние (e5f2c1a · 14:20)",
        None,
        (),
    )
    assert revert_target_line(head_with_ref) == (
        "вернёт к закоммиченному состоянию (e5f2c1a · 14:20)"
    )

    head_bare = RevertTarget(
        "a.py", "head", "закоммиченное состояние", None, ()
    )
    assert revert_target_line(head_bare) == "вернёт к закоммиченному состоянию"

    session = RevertTarget("a.py", "session", "начало сессии", None, ())
    assert revert_target_line(session) == "вернёт к началу сессии"

    turn = RevertTarget("a.py", "turn", "начало хода", None, ())
    assert revert_target_line(turn) == "вернёт к началу хода"

    # То же самое живьём на экране — для «принято», со своим цветом.
    ws = _Workspace(tmp_path / "accepted")
    ws.write("acc.py", "v1\n")
    ws.journal.append(_rec("acc.py", ts=1.0, op="create"))
    view = EditsView(
        str(ws.threads_dir), "cli", "th1", str(ws.root), as_settings(ws.config)
    )
    view.accept(["acc.py"])
    accepted_hhmm = (
        f"{datetime.fromtimestamp(ws.store.accepted_state('acc.py').ts):%H:%M}"
    )
    ws.write("acc.py", "v2\n")
    ws.journal.append(_rec("acc.py", ts=2.0, call_id="c2"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        expected = f"вернёт к принятому состоянию ({accepted_hhmm})"
        body = _body(confirm)
        assert expected in body

        line = next(
            w
            for w in confirm.query("#trace > Static")
            if expected in _plain(w)
        )
        assert span_style(line.content, "вернёт") == ACCENT

    # ...и живьём — для «закоммичено» (Р13 ревью: `revert_target_line` для
    # `head` до этой правки гонялся только через ручные `RevertTarget`, а не
    # через реальный `EditsView.revert_targets`, — тут именно это).
    import subprocess

    from gc_core.agent_edits import git_ops

    wsh = _Workspace(tmp_path / "head")
    _git_init(wsh.root)
    wsh.write("head.py", "orig\n")
    subprocess.run(
        ["git", "add", "--", "head.py"],
        cwd=str(wsh.root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "seed"],
        cwd=str(wsh.root),
        check=True,
        capture_output=True,
    )
    wsh.base("head.py", "orig\n")
    wsh.write("head.py", "orig\n" + "".join(f"line{i}\n" for i in range(40)))
    wsh.journal.append(_rec("head.py", ts=1.0, add=40, del_=0))
    subprocess.run(
        ["git", "add", "--", "head.py"],
        cwd=str(wsh.root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "commit +40"],
        cwd=str(wsh.root),
        check=True,
        capture_output=True,
    )
    with open(wsh.root / "head.py", "a", encoding="utf-8", newline="\n") as fh:
        fh.write("".join(f"more{i}\n" for i in range(10)))
    wsh.journal.append(_rec("head.py", ts=2.0, call_id="c2", add=10, del_=0))
    expected_ref = git_ops.head_commit_label(str(wsh.root), "head.py")

    async with TuiApp(
        threads_dir=str(wsh.threads_dir),
        system="cli",
        settings=tui_settings(wsh.config),
    ).run_test() as pilot:
        await _open(pilot, wsh.screen())
        await pilot.press("r")
        await pilot.pause()
        assert f"вернёт к закоммиченному состоянию ({expected_ref})" in _body(
            pilot.app.screen
        )

    # ...и живьём — для «начало хода» (Р13 ревью: одиночный `r`, не только
    # стрелка массового превью, которая в словарь падежей не заглядывает).
    wst = _Workspace(tmp_path / "turn")
    wst.store.capture_turn("t.py", b"before\n", "t1")
    wst.write("t.py", "after\n")
    wst.journal.append(_rec("t.py", turn="t1", ts=1.0))

    async with TuiApp(
        threads_dir=str(wst.threads_dir),
        system="cli",
        settings=tui_settings(wst.config),
    ).run_test() as pilot:
        await _open(pilot, wst.screen(source=SOURCE_TURN))
        await pilot.press("r")
        await pilot.pause()
        assert "вернёт к началу хода" in _body(pilot.app.screen)


def test_reset_target_line_names_the_remaining_step():
    """Шапка одиночного подтверждения сброса — родительный падеж после
    предлога «от» (задача 4 доп., С7): тот же `RevertTarget`, что и у отката,
    но сброс достаёт только «head»/«session» — принятой ступени после сброса
    уже нет, а «Ход» сброс не видит."""
    from gc_tui.services.edits_view import RevertTarget

    head_with_ref = RevertTarget(
        "a.py",
        "head",
        "закоммиченное состояние (e5f2c1a · 14:20)",
        None,
        (),
    )
    assert reset_target_line(head_with_ref) == (
        "после сброса счёт пойдёт от закоммиченного состояния (e5f2c1a · 14:20)"
    )

    head_bare = RevertTarget(
        "a.py", "head", "закоммиченное состояние", None, ()
    )
    assert reset_target_line(head_bare) == (
        "после сброса счёт пойдёт от закоммиченного состояния"
    )

    session = RevertTarget("a.py", "session", "начало сессии", None, ())
    assert (
        reset_target_line(session)
        == "после сброса счёт пойдёт от начала сессии"
    )


def test_reset_loss_line_names_the_price_of_the_reset():
    """Д5 второго смоука: подтверждение сброса обязано сказать, ЧТО теряется,
    а не только откуда пойдёт счёт. Время принятия — тем же форматом, что и
    расшифровка глифа `↺` в шапке вьюера (`accepted_caption`).

    Времени может не быть вовсе (принятую базу не удалось прочитать —
    `reset_targets` всё равно считает точку теряемой): строка обязана прожить
    без часа, а не выдумать его.
    """
    from gc_tui.services.edits_view import ResetTarget, RevertTarget

    session = RevertTarget("a.py", "session", "начало сессии", None, ())
    ts = time.mktime((2026, 8, 21, 14, 20, 0, 0, 0, -1))

    assert reset_loss_line(ResetTarget("a.py", session, True, ts)) == (
        "точка отката к принятому состоянию (14:20) будет потеряна"
    )
    assert (
        reset_loss_line(ResetTarget("a.py", session, False, ts))
        == "терять нечего"
    )
    assert reset_loss_line(ResetTarget("a.py", session, True, None)) == (
        "точка отката к принятому состоянию будет потеряна"
    )


def test_target_dative_stem_covers_every_ladder_kind():
    """Страж полноты словаря (Р13 ревью): переименование kind-строки в
    сервисе обязано сломать ЭТОТ тест, а не молча срезать «вернёт к» с экрана.

    `accepted`/`head` сверяются с настоящими константами `agent_edits.ladder`
    — рассинхрон там ловится напрямую. `session`/`turn` в сервисе своих
    констант не имеют (голые литералы и там, и здесь) — фиксируются как есть,
    тем не менее пин здесь ловит случайную опечатку в САМОМ словаре.
    """
    from gc_core.agent_edits.ladder import STEP_ACCEPTED, STEP_HEAD
    from gc_tui.screens.diff import _TARGET_DATIVE_STEM

    assert set(_TARGET_DATIVE_STEM) == {
        STEP_ACCEPTED,
        STEP_HEAD,
        "session",
        "turn",
    }


def test_reset_target_genitive_stem_covers_every_reachable_kind():
    """Тот же страж полноты, что у `_TARGET_DATIVE_STEM` (Р13 ревью), для
    словаря сброса: он короче отката НЕ случайно — `reset_targets`
    (`tui.services.edits_view`) никогда не возвращает «accepted»/«turn»/
    «delete» (принятой ступени после сброса уже нет, «Ход» сброс не видит,
    а сброс ничего не удаляет), и переименование одной из двух оставшихся
    kind-строк обязано сломать ЭТОТ тест, а не молча срезать текст цели.
    """
    from gc_core.agent_edits.ladder import STEP_HEAD
    from gc_tui.screens.diff import _RESET_TARGET_GENITIVE_STEM

    assert set(_RESET_TARGET_GENITIVE_STEM) == {STEP_HEAD, "session"}


def _accepted_hhmm(ws: _Workspace, rel: str) -> str:
    return time.strftime(
        "%H:%M", time.localtime(ws.store.accepted_state(rel).ts)
    )


@pytest.mark.asyncio
async def test_single_u_screen_has_no_name_row_and_names_the_loss(tmp_path):
    """`u` (Д5): имя — в шапке, а тело говорит обе правды сразу — что теряется
    (точка отката к принятому состоянию) и откуда после этого пойдёт счёт.

    Блок одиночного набора строки имени не получает (Е3) — тело начинается
    сразу с потери. Полотно короткое и в кадр помещается целиком — мёртвая
    прокрутка в полосе не рекламируется.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")
    ws.write("a.py", "v2\n")  # дописано после принятия — точка теряется

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("u")
        await pilot.pause()

        confirm = pilot.app.screen
        assert (
            _plain(confirm.query_one("#header-left"))
            == "Сброс принятия · a.py"
        )
        lines = [line for line in _body(confirm).splitlines() if line.strip()]
        assert lines == [
            f"точка отката к принятому состоянию ({_accepted_hhmm(ws, 'a.py')})"
            " будет потеряна",
            "после сброса счёт пойдёт от начала сессии",
        ]
        assert confirm.query("#trace > .tree-head").nodes == []
        assert _plain(confirm.query_one("#footer")) == (
            "Enter сбросить 1 файл · Esc отмена"
        )


@pytest.mark.asyncio
async def test_reset_tree_lists_every_file_as_a_block_without_dashes(tmp_path):
    """`Ctrl+U` (Д5/R5 + Е3): подтверждение перечисляет ВСЕХ, у кого снимается
    принятие, — и того, кто ничем не рискует, тоже. Шапка считает всех: старое
    «у N файлов» по одним лишь теряющим занижало число, а тело молчало о цене.

    Тело — дерево блоков: строка имени без обрамления тире и под ней две
    вложенные строки (цена и последствие); блоки разделены ровно одной пустой
    строкой, других зазоров нет.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("lost.py", "v1\n")
    ws.journal.append(_rec("lost.py", ts=2.0))
    ws.accept("lost.py", "v1\n")
    ws.write("lost.py", "v2\n")  # дельта непуста — точка отката теряется

    ws.write("kept.py", "v1\n")
    ws.journal.append(_rec("kept.py", ts=1.0, call_id="c2"))
    ws.accept("kept.py", "v1\n")  # диск равен принятому — терять нечего

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+u")
        await pilot.pause()

        confirm = pilot.app.screen
        assert (
            _plain(confirm.query_one("#header-left"))
            == "Сброс принятия у 2 файлов"
        )
        loss = (
            f"точка отката к принятому состоянию ({_accepted_hhmm(ws, 'lost.py')})"
            " будет потеряна"
        )
        goes_from = "после сброса счёт пойдёт от начала сессии"
        assert [
            line for line in _body(confirm).splitlines() if line.strip()
        ] == [
            "lost.py",
            loss,
            goes_from,
            "kept.py",
            "терять нечего",
            goes_from,
        ]
        # Ни одна строка не обрамлена тире — блок держится отступом, не рамкой.
        assert all(
            not line.startswith("— ") and not line.endswith(" —")
            for line in _body(confirm).splitlines()
        )

        # Классы дерева: имя — голова блока, цена — предупреждение (только там,
        # где действительно теряют), последствие — вложенная строка под ними.
        # Класс дерева на виджете РОВНО один: отступы задаёт он один своим
        # shorthand'ом, и вторая такая же пара правил их бы перетёрла.
        def _tree_class(widget) -> str:
            kinds = sorted(c for c in widget.classes if c.startswith("tree-"))
            assert len(kinds) == 1, (_plain(widget), kinds)
            return kinds[0]

        assert [
            (_plain(w), _tree_class(w))
            for w in confirm.query("#trace > Static")
        ] == [
            ("lost.py", "tree-head"),
            (loss, "tree-warn"),
            (goes_from, "tree-sub"),
            ("", "tree-gap"),
            ("kept.py", "tree-head"),
            ("терять нечего", "tree-line"),
            (goes_from, "tree-sub"),
        ]
        # Между блоками ровно одна пустая строка — её и даёт `tree-gap`.
        assert _body(confirm).count("\n\n") == 1
        assert _plain(confirm.query_one("#footer")) == confirm_footer(
            count=2, can_scroll=False, reset=True
        )

        await pilot.press("enter")
        await pilot.pause()

        assert ws.store.accepted_state("lost.py") is None
        assert ws.store.accepted_state("kept.py") is None


@pytest.mark.asyncio
async def test_reset_confirm_advertises_scrolling_only_when_the_body_scrolls(
    tmp_path,
):
    """Д5: `↑↓ PgUp PgDn` в полосе — только когда телу есть куда ехать.

    Замер, а не догадка: тело из шести файлов в кадр не влезает — прокрутка
    живая и названа; кадр вырос по высоте, то же тело поместилось целиком —
    сегмент уходит. Ширина при этом не менялась ни разу: полоса обязана
    пересобираться и на `Resize`, от которого не зависят ни тело, ни шапка.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(6):
        rel = f"f{idx}.py"
        ws.write(rel, "v1\n")
        ws.journal.append(_rec(rel, ts=float(idx), call_id=f"c{idx}"))
        ws.accept(rel, "v1\n")
        ws.write(rel, "v2\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(100, 24)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+u")
        await pilot.pause()

        confirm = pilot.app.screen
        assert confirm.query_one("#trace").max_scroll_y > 0
        assert _plain(confirm.query_one("#footer")) == (
            "Enter сбросить 6 файлов · ↑↓ PgUp PgDn · Esc отмена"
        )

        # Кадр вырос по ВЫСОТЕ — тело поместилось целиком, и полоса про это
        # знает: ширина не менялась, а живость прокрутки сменилась.
        await pilot.resize_terminal(100, 60)
        await pilot.pause()

        assert confirm.query_one("#trace").max_scroll_y == 0
        assert _plain(confirm.query_one("#footer")) == (
            "Enter сбросить 6 файлов · Esc отмена"
        )


@pytest.mark.asyncio
async def test_reset_screen_has_no_cursor_and_ignores_n_p(tmp_path):
    """Е3 в теле сброса: курсора по файлам нет, `n`/`p` мертвы.

    Тот же документ, что и у отката, но полотно другое — и цена ошибки та же:
    кромка над файлом обещала бы «Enter над выделенным», хотя сброс всегда
    снимает принятие у всего набора.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for idx in range(6):
        rel = f"f{idx}.py"
        ws.write(rel, "v1\n")
        ws.journal.append(_rec(rel, ts=float(idx), call_id=f"c{idx}"))
        ws.accept(rel, "v1\n")
        ws.write(rel, "v2\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(100, 24)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("ctrl+u")
        await pilot.pause()

        confirm = pilot.app.screen
        trace = confirm.query_one("#trace")
        assert len(confirm.query("#trace > .tree-head")) == 6

        await pilot.press("n")
        await pilot.pause()
        assert confirm.query("#trace > .cursor").nodes == []
        assert trace.scroll_y == 0  # прыжка по файлам нет
        assert pilot.app.screen is confirm

        await pilot.press("p")
        await pilot.pause()
        assert confirm.query("#trace > .cursor").nodes == []
        assert trace.scroll_y == 0

        footer = _plain(confirm.query_one("#footer"))
        assert "n след. файл" not in footer and "p пред." not in footer


@pytest.mark.asyncio
async def test_bulk_preview_targets_and_warns(tmp_path):
    """Массовое превью: цель — стрелкой у каждого файла (§4.4), падеж ей не
    нужен; `⚠`-строки — только у путей «Хода», которых заденет ступень,
    возникшая после начала хода.
    """
    from gc_tui.app import TuiApp
    from gc_tui.services.edits_view import EditsView

    # --- «Сессия»: у файлов разные ступени — разные стрелки, без ⚠ ---------
    ws1 = _Workspace(tmp_path / "session")
    ws1.base("plain.py", "before\n")
    ws1.write("plain.py", "after\n")
    ws1.journal.append(_rec("plain.py", ts=1.0))

    ws1.write("acc.py", "v1\n")
    ws1.journal.append(_rec("acc.py", ts=1.0, op="create"))
    view1 = EditsView(
        str(ws1.threads_dir),
        "cli",
        "th1",
        str(ws1.root),
        as_settings(ws1.config),
    )
    view1.accept(["acc.py"])
    accepted_hhmm = f"{datetime.fromtimestamp(ws1.store.accepted_state('acc.py').ts):%H:%M}"
    ws1.write("acc.py", "v2\n")
    ws1.journal.append(_rec("acc.py", ts=2.0, call_id="c2"))

    async with TuiApp(
        threads_dir=str(ws1.threads_dir),
        system="cli",
        settings=tui_settings(ws1.config),
    ).run_test() as pilot:
        await _open(pilot, ws1.screen())
        await pilot.press("ctrl+r")
        await pilot.pause()
        body = _body(pilot.app.screen)
        assert f"→ принятое состояние ({accepted_hhmm})" in body
        assert "→ начало сессии" in body
        assert "⚠" not in body

    # --- «Ход»: у обоих файлов одна цель, ⚠ — только у задетого принятием --
    ws2 = _Workspace(tmp_path / "turn")
    ws2.store.capture_turn("a.py", b"turn-a\n", "t1")
    ws2.write("a.py", "now-a\n")
    ws2.journal.append(_rec("a.py", turn="t1", ts=1.0))
    ws2.store.capture_turn("b.py", b"turn-b\n", "t1")
    ws2.write("b.py", "now-b\n")
    ws2.journal.append(_rec("b.py", turn="t1", ts=1.0, call_id="c2"))

    view2 = EditsView(
        str(ws2.threads_dir),
        "cli",
        "th1",
        str(ws2.root),
        as_settings(ws2.config),
    )
    view2.accept(["b.py"])
    accepted_hhmm2 = (
        f"{datetime.fromtimestamp(ws2.store.accepted_state('b.py').ts):%H:%M}"
    )

    async with TuiApp(
        threads_dir=str(ws2.threads_dir),
        system="cli",
        settings=tui_settings(ws2.config),
    ).run_test() as pilot:
        await _open(pilot, ws2.screen(source=SOURCE_TURN))
        await pilot.press("ctrl+r")
        await pilot.pause()
        body = _body(pilot.app.screen)
        assert body.count("→ начало хода") == 2
        warn = (
            f"⚠ заденет принятое вами состояние (принято в {accepted_hhmm2})"
        )
        assert body.count(warn) == 1


@pytest.mark.asyncio
async def test_session_preview_never_warns(tmp_path):
    """«Сессия» не опускается ниже своей ступени по построению (§4.4) — `⚠`
    там невозможен, даже когда файл принят и правился заново после этого.
    """
    from gc_tui.app import TuiApp
    from gc_tui.services.edits_view import EditsView

    ws = _Workspace(tmp_path)
    ws.write("acc.py", "v1\n")
    ws.journal.append(_rec("acc.py", ts=1.0, op="create"))
    view = EditsView(
        str(ws.threads_dir), "cli", "th1", str(ws.root), as_settings(ws.config)
    )
    view.accept(["acc.py"])
    ws.write("acc.py", "v2\n")
    ws.journal.append(_rec("acc.py", ts=2.0, call_id="c2"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        assert "⚠" not in _body(pilot.app.screen)


@pytest.mark.asyncio
async def test_absent_target_says_delete(tmp_path):
    """Создан агентом → ступени «выше» нет вовсе: превью прямо говорит про
    удаление, а не «вернёт к» — цели-состояния для такого пути не существует.
    """
    from gc_tui.app import TuiApp
    from gc_tui.palette import ACCENT

    ws = _Workspace(tmp_path)
    ws.write("new.py", "сочинено\n")
    ws.journal.append(_rec("new.py", op="create", add=1, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        confirm = pilot.app.screen
        body = _body(confirm)
        assert "будет удалён (создан агентом)" in body
        assert "вернёт" not in body

        line = next(
            w
            for w in confirm.query("#trace > Static")
            if "будет удалён" in _plain(w)
        )
        assert span_style(line.content, "будет удалён") == ACCENT


# --- гейты и последствия -----------------------------------------------------


@pytest.mark.asyncio
async def test_revert_blocked_while_run_is_active(tmp_path):
    """Живой прогон пишет те же файлы — откат в это время гонка, а не откат."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "исходное\n")
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        pilot.app.active_runner = SimpleNamespace(is_running=lambda: True)
        notes: list[tuple[str, str]] = []
        pilot.app.notify = lambda text, **kw: notes.append(
            (text, kw.get("severity"))
        )

        await pilot.press("r")
        await pilot.pause()
        assert pilot.app.screen is scr  # подтверждения нет
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert pilot.app.screen is scr
        assert notes == [(BUSY_REVERT, "warning"), (BUSY_REVERT, "warning")]
        assert ws.read("a.py") == "правленое\n"


@pytest.mark.asyncio
async def test_list_refreshes_after_revert(tmp_path):
    """Откат стёр правку — строке на экране изменений места нет.

    Прежде она оставалась с `+0 −0`. Такая строка мертва целиком: содержимого
    нет, `a` нечего принимать, `r` отвечает «отменять нечего», — а десяток
    таких вытесняет настоящие правки за край экрана. Данные перечитываются ДО
    курсора, поэтому и список, и итог в шапке обязаны сойтись сразу.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a\n")
    ws.write("a.py", "a\nb\nc\n")
    ws.journal.append(_rec("a.py", add=2, del_=0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert "+2" in _plain(scr._rows[0])

        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert scr._entries == []
        # Шапка не показывает нули, а говорит прямо: считать больше нечего.
        assert "изменений нет" in _plain(scr.query_one("#header-left"))


@pytest.mark.asyncio
async def test_confirm_does_not_overwrite_an_edit_made_after_the_preview(
    tmp_path,
):
    """Файл поправили, пока открыто подтверждение, — `Enter` его не затирает.

    Превью показывало одно, а откат записал бы базу поверх того, чего
    пользователь не видел: правка в редакторе, сделанная при открытом окне,
    пропадала молча (ревью PR, находка 6). Подтверждение сверяет файлы с тем,
    что было при показе, и при расхождении не пишет ничего."""
    import os

    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import REVERT_STALE

    ws = _Workspace(tmp_path)
    ws.base("a.py", "original\n")
    ws.write("a.py", "agent\n")
    disk = ws.root / "a.py"
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RevertConfirmScreen)

        disk.write_text("manual-after-preview\n", encoding="utf-8")
        st = disk.stat()
        os.utime(disk, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        await pilot.press("enter")
        await pilot.pause()

        assert disk.read_text(encoding="utf-8") == "manual-after-preview\n"
        assert any(REVERT_STALE in n.message for n in pilot.app._notifications)
        assert pilot.app.screen is scr


@pytest.mark.asyncio
async def test_confirm_does_not_revert_when_the_target_changed_after_the_preview(
    tmp_path,
):
    """Цель отката сменилась в другом окне — `Enter` не пишет ничего.

    Диск не менялся, а цель — да: превью обещало вернуть файл к принятому
    состоянию, другой экземпляр снял принятие, и откат записал бы начало
    сессии, которого пользователь в превью не видел (ревью починок, находка 1).
    Сверяется не только диск, но и индекс снимков, где живут принятия."""
    from gc_tui.app import TuiApp
    from gc_tui.screens.diff import REVERT_STALE
    from gc_tui.services.edits_view import EditsView

    ws = _Workspace(tmp_path)
    ws.base("a.py", "original\n")
    ws.accept("a.py", "accepted\n")
    ws.write("a.py", "agent\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RevertConfirmScreen)

        other = EditsView(
            str(ws.threads_dir), "cli", "th1", str(ws.root), as_settings({})
        )
        other.reset_accept(["a.py"])
        await pilot.press("enter")
        await pilot.pause()

        assert ws.read("a.py") == "agent\n"
        assert any(REVERT_STALE in n.message for n in pilot.app._notifications)
        assert pilot.app.screen is scr


@pytest.mark.asyncio
async def test_revert_from_viewer_returns_to_list(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "исходное\n")
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.base("b.py", "b\n")
    ws.write("b.py", "b\nb2\n")
    ws.journal.append(_rec("b.py", ts=2.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("right")  # ушли на a.py
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)
        assert viewer.entry.path == "a.py"

        await pilot.press("r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RevertConfirmScreen)
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr  # вьюер закрылся сам
        assert ws.read("a.py") == "исходное\n"
        # Откаченный файл ушёл со списка целиком — остался только соседний.
        assert [e.path for e in scr._entries] == ["b.py"]


@pytest.mark.asyncio
async def test_ctrl_r_is_not_a_viewer_key_but_stays_on_the_list(tmp_path):
    """Р20: массовый откат живёт там, где виден весь набор, — только в списке.

    Во вьюере `Ctrl+R` работал молча: в футере его не было, а весь набор он
    откатывал. Разрушающее действие, о котором пользователь не знает.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "a-до\n")
    ws.write("a.py", "a-после\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.base("b.py", "b-до\n")
    ws.write("b.py", "b-после\n")
    ws.journal.append(_rec("b.py", ts=2.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)

        await pilot.press("ctrl+r")
        await pilot.pause()

        assert pilot.app.screen is viewer  # подтверждения нет
        assert ws.read("a.py") == "a-после\n"
        assert ws.read("b.py") == "b-после\n"

        # А в списке клавиша на месте — вьюер её только у себя не держит.
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen is scr
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RevertConfirmScreen)


@pytest.mark.asyncio
async def test_cancelled_revert_from_viewer_keeps_the_viewer_open(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "исходное\n")
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()
        viewer = pilot.app.screen
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert pilot.app.screen is viewer
        assert ws.read("a.py") == "правленое\n"


@pytest.mark.asyncio
async def test_turn_source_reverts_to_the_turn_baseline(tmp_path):
    """Источник задаёт базу: «Ход» откатывает к состоянию до последнего хода."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.base("a.py", "сессия\n")
    ws.store.capture_turn("a.py", "ход\n".encode(), "t2")
    ws.write("a.py", "сейчас\n")
    ws.journal.append(_rec("a.py", turn="t1", ts=1.0))
    ws.journal.append(_rec("a.py", turn="t2", ts=2.0))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen(source=SOURCE_TURN))
        assert scr.source == SOURCE_TURN
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert ws.read("a.py") == "ход\n"


@pytest.mark.asyncio
async def test_undo_feedback_reaches_the_thread_as_technical_message(tmp_path):
    """T15: после отката агент обязан узнать, что его картина мира устарела."""
    from gc_core.message_policy.user_message_policy import (
        is_technical_user_message_text,
    )
    from gc_testkit.develop_files import load_thread_messages
    from gc_testkit.develop_files import append_new_thread_messages
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    append_new_thread_messages(
        str(ws.threads_dir),
        "cli",
        "th1",
        [{"role": "user", "content": "привет", "id": "m1"}],
        set(),
    )
    ws.base("a.py", "исходное\n")
    ws.write("a.py", "правленое\n")
    ws.journal.append(_rec("a.py"))

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test() as pilot:
        trace = pilot.app.screen
        trace.attach_active_task("th1")
        await pilot.pause()
        await _open(
            pilot,
            ws.screen(
                on_undo_feedback=trace._on_diff_undo_feedback,
                on_ui_ack=trace._on_diff_ui_ack,
            ),
        )
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        msgs, _keys, _seq = load_thread_messages(
            str(ws.threads_dir), "cli", "th1"
        )
        technical = [
            m
            for m in msgs
            if is_technical_user_message_text(m.get("content", ""))
        ]
        assert len(technical) == 1
        assert "a.py" in technical[0]["content"]
        assert "Changes reverted" in technical[0]["content"]


@pytest.mark.asyncio
async def test_text_file_overwritten_by_binary_is_restorable(
    tmp_path, monkeypatch
):
    """Сквозной путь на настоящем рекордере: право на откат даёт снимок, не `op`.

    Тул правит текстовый файл — рекордер снимает точный `.orig`; следом тот же
    путь перезаписывают бинарём (`op="binary"`, дельту считать нечем). Откат
    обязан вернуть текст, хотя последняя запись пути «несчётная».
    """
    from gc_core.agent_edits.recorder import record_edit
    from gc_core.config_modules import env_vars
    from gc_tui.services.edits_view import EditsView

    ws = _Workspace(tmp_path)
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    from gc_testkit.settings import fake_ctx

    ctx = fake_ctx(
        {"launch_root": str(ws.root)},
        is_tui=True,
        threads_dir=str(ws.threads_dir),
        system_id="cli",
        thread_id="th1",
        turn_id="t1",
    )
    target = ws.root / "a.txt"
    old = "текст\n".encode()
    new = "текст\nдописано\n".encode()
    target.write_bytes(new)
    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=old,
        new_bytes=new,
        op="edit",
        call_id="c1",
    )
    binary = b"\x89PNG\r\n\x1a\n\x00\x01"
    target.write_bytes(binary)
    await record_edit(
        ctx,
        abs_path=str(target),
        old_bytes=new,
        new_bytes=binary,
        op="edit",
        call_id="c2",
    )

    view = EditsView(
        str(ws.threads_dir), "cli", "th1", str(ws.root), as_settings(ws.config)
    )
    entry = next(
        row.entry
        for row in view.session_view(SOURCE_SESSION).active
        if row.entry.path == "a.txt"
    )
    assert entry.countable is False and entry.revertable is True

    outcome = view.revert(SOURCE_SESSION, ["a.txt"])
    assert outcome.restored == ["a.txt"]
    assert target.read_bytes() == old


# --- большие наборы: компактное подтверждение -------------------------------


def _many_edited(ws, n: int, *, accepted: bool = False) -> list[str]:
    paths = [f"f{i:02d}.py" for i in range(n)]
    for i, rel in enumerate(paths):
        if accepted:
            ws.accept(rel, "old\n")
        else:
            ws.base(rel, "old\n")
        ws.write(rel, "new\n")
        ws.journal.append(_rec(rel, ts=float(i), call_id=f"c{i}"))
    return paths


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ctrl+r", "ctrl+u"])
async def test_confirmation_of_a_big_set_names_every_file_without_bodies(
    tmp_path, monkeypatch, key
):
    """Большой набор — компактное подтверждение: каждый файл назван, тел диффов нет.

    Полное подтверждение рисует дифф каждого файла, а ленивое тело всё равно
    держит виджет на каждый участок: 10 000 файлов — около 160 тысяч виджетов,
    15 с на открытие и 5 с на PgDn (смок 2026-09-24). Выше порога набор
    называется поимённо плотным текстом — несколькими виджетами, — а дифф
    любого файла открывается на экране изменений. Набор НЕ режется."""
    pytest.importorskip("textual")
    import gc_tui.screens.diff_revert_confirm as mod
    from gc_tui.app import TuiApp

    monkeypatch.setattr(mod, "FULL_PREVIEW_MAX_FILES", 3)
    ws = _Workspace(tmp_path)
    paths = _many_edited(ws, 5, accepted=key == "ctrl+u")

    async with TuiApp(
        threads_dir=str(ws.threads_dir),
        system="cli",
        settings=tui_settings(ws.config),
    ).run_test(size=(120, 40)) as pilot:
        await _open(pilot, ws.screen())
        await pilot.press(key)
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        body = _body(confirm)
        widgets = list(confirm.query("#trace > Static"))

        assert all(path in body for path in paths)
        assert mod.COMPACT_NOTE.format(limit=3).split(" —")[0] in body
        assert not any(w.has_class("diff-hunk") for w in widgets)
        assert len(widgets) <= 6


def _session_target(path: str):
    from gc_tui.services.edits_view import RevertTarget

    return RevertTarget(path, "session", "начало сессии", None, ())


def test_compact_revert_keeps_every_note_of_the_full_view():
    """Компактный откат несёт те же пояснения, что полный, — без тел диффов.

    Форма общая с компактным сбросом: строка файла — имя, счётчики и цель;
    под ней с отступом всё, что о файле надо знать. Несчётный путь без
    «превью недоступно» выглядел голым именем среди строк с числами — как сбой
    вывода, а путь без различий терял «отменять нечего» и читался как `+0 −0`
    (ревью `15eb53bed`)."""
    from gc_core.agent_edits.deltas import FileDelta
    from gc_tui.palette import WARN

    screen = RevertConfirmScreen(
        paths=["a.bin", "same.py", "c.py"],
        add=1,
        del_=1,
        exact=False,
        skipped=["s.py"],
        external=["c.py"],
        preview=[
            ("a.bin", FileDelta(0, 0, "", countable=False)),
            ("same.py", FileDelta(0, 0, "")),
            ("c.py", FileDelta(1, 1, "@@ -1 +1 @@\n-a\n+b\n")),
        ],
        targets=[_session_target(p) for p in ("a.bin", "same.py", "c.py")],
    )

    lines = screen._compact_revert_lines()

    assert [line.plain for line in lines] == [
        "a.bin  → начало сессии",
        f"  {VIEWER_NO_PREVIEW}",
        "same.py · +0 −0  → начало сессии",
        f"  {PREVIEW_NOTHING}",
        "c.py · +1 −1  → начало сессии",
        f"  {WARN_EXTERNAL}",
        "s.py",
        f"  {SKIPPED_LINE}",
    ]
    assert lines[5].style == WARN


def test_compact_reset_has_the_same_form_as_compact_revert():
    """Компактный сброс — той же формы: цель в строке файла, цена строкой под ней.

    Раньше цена и цель были склеены в одну строку, и цена сброса читалась
    только цветом, а у отката предупреждение стоит своей строкой под именем
    (ревью `15eb53bed`)."""
    from gc_tui.palette import WARN
    from gc_tui.services.edits_view import ResetTarget

    losing = ResetTarget("a.py", _session_target("a.py"), True, None)
    nothing = ResetTarget("b.py", _session_target("b.py"), False, None)
    screen = RevertConfirmScreen.for_reset(
        paths=["a.py", "b.py"],
        targets=[losing, nothing],
        mass=True,
    )

    lines = screen._compact_reset_lines()

    assert [line.plain for line in lines] == [
        f"a.py  {reset_target_line(losing.target)}",
        f"  {reset_loss_line(losing)}",
        f"b.py  {reset_target_line(nothing.target)}",
        f"  {reset_loss_line(nothing)}",
    ]
    assert lines[1].style == WARN
    assert lines[3].style != WARN
