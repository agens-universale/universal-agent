"""Принятие и отмена принятия с экрана изменений: `a`/`Ctrl+A` — файл/всё
представление, `u`/`Ctrl+U` — снять принятие одного файла/всех принятых
вручную.

Принятие — не «застейджить», а сдвиг точки отсчёта (пересмотр Р9 волны 4):
``.accepted`` := диск, затем, в git-проекте, best-effort ``git add``.
В отличие от старого git add принятие МЕНЯЕТ данные экрана, и здесь
проверяется именно это:

* база действительно сдвинулась (``SnapshotStore.accepted_state``), а не
  «файл просто оказался в индексе»;
* принятый файл, если дельта от новой базы пуста, в тот же миг уезжает в
  группу «принято» — список и счётчики перечитываются, а не остаются
  прежними, как было у git add (Р9);
* путь оказался в ИНДЕКСЕ git (настоящий репозиторий, не мок) — best-effort
  сторона принятия та же, что была у старого действия;
* отмена принятия (`u`/`Ctrl+U`) просит подтверждение, только когда дельта от
  принятой базы непуста — иначе повторное `a` восстановило бы то же
  состояние, и спрашивать нечего;
* во время живого прогона все четыре действия отклоняются тем же гейтом, что
  откат.

Итог живёт в канонной плашке экрана (`.set-flash-ok`/`-warn`/`-note`), как и
раньше: плашка идемпотентна и переживает перечитывание списка. Часть тестов
файла — про откат (`r`), унаследованы от git-add-файла как есть:
инфраструктура (`_Workspace`, `_flash_text`) общая, а сам откат этой задачей
не тронут.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.ladder import STEP_ACCEPTED
from gc_core.agent_edits.snapshots import SnapshotStore, snapshots_root
from gc_core.thread_store import edits_journal_path
from gc_tui.screens.diff import (
    ACCEPT_NEW_BASELINE,
    ACCEPT_STAGE_ACTION,
    ACCEPT_STAGE_FAILED,
    BUSY_ACTION,
    DiffFileViewer,
    DiffScreen,
)
from gc_tui.services.edits_view import SOURCE_TURN
from gc_tui.screens.diff_revert_confirm import RevertConfirmScreen
from gc_tui.palette import DEEP_DIM, DIM

pytest.importorskip("textual")


# --- фикстуры данных ---------------------------------------------------------


def _rec(path="a.py", turn="t1", call_id="c1", op="edit", add=1, del_=1, ts=1.0):
    return EditRecord(
        path=path, turn_id=turn, ts=ts, call_id=call_id, op=op, hunks="",
        add=add, del_=del_, before_sha256=None, after_sha256=None, flags=[],
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True)


class _Workspace:
    """Тред с журналом и снимками + файлы под launch_root; по желанию — репозиторий."""

    def __init__(self, tmp_path: Path, *, git: bool = False) -> None:
        self.threads_dir = tmp_path / "threads"
        self.threads_dir.mkdir(parents=True)
        self.root = tmp_path / "proj"
        self.root.mkdir(parents=True)
        if git:
            if shutil.which("git") is None:
                pytest.skip("git недоступен")
            for args in (
                ("init", "-b", "main"),
                ("config", "user.email", "test@example.com"),
                ("config", "user.name", "test"),
                ("config", "core.autocrlf", "false"),
            ):
                _git(self.root, *args)
        self.journal = EditsJournal(
            edits_journal_path(str(self.threads_dir), "cli", "th1")
        )
        self.store = SnapshotStore(
            snapshots_root(str(self.root)), "cli_th1", self.threads_dir
        )
        self.acks: list[str] = []

    @property
    def config(self) -> dict:
        return {"launch_root": str(self.root), "model": "M", "agent_edits": {}}

    def write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def base(self, rel: str, text: str) -> None:
        self.store.capture_session(rel, text.encode("utf-8"))

    def accept(self, rel: str, text: str) -> None:
        """Принять путь «вручную» мимо экрана — готовый снимок для фикстур."""
        self.store.capture_accepted(rel, text.encode("utf-8"), head_oid=None)

    def staged(self) -> set[str]:
        out = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(self.root), capture_output=True, check=True,
        )
        return {line for line in out.stdout.decode("utf-8").splitlines() if line}

    def edit(self, rel: str, before: str, after: str, **kwargs) -> None:
        """Правка агента «как в жизни»: точный снимок + файл на диске + запись."""
        self.base(rel, before)
        self.write(rel, after)
        self.journal.append(_rec(rel, **kwargs))

    def screen(self, **kwargs) -> DiffScreen:
        kwargs.setdefault("on_ui_ack", self.acks.append)
        return DiffScreen(
            launch_root=str(self.root),
            config=self.config,
            threads_dir=str(self.threads_dir),
            system_id="cli",
            thread_id="th1",
            **kwargs,
        )


def _plain(widget) -> str:
    return str(widget.render())


def _body(screen) -> str:
    """Тело полноэкранного подтверждения — тот же приём, что в `test_diff_revert.py`."""
    return "\n".join(_plain(w) for w in screen.query("#trace > Static"))


def _flashes(screen) -> list:
    """Видимые плашки: снятая живёт в DOM спрятанной (см. `_clear_flash`)."""
    return [
        w for w in screen.query("Static")
        if w.display and (w.has_class("set-flash-ok")
                          or w.has_class("set-flash-warn")
                          or w.has_class("set-flash-note"))
    ]


def _flash_text(screen) -> str:
    flashes = _flashes(screen)
    assert len(flashes) == 1, [_plain(w) for w in flashes]
    return _plain(flashes[0])


def _screen_lines(app) -> list[str]:
    """Текстовый слепок ВИДИМОГО экрана строками композитора."""
    return ["".join(seg.text for seg in strip).rstrip()
            for strip in app.screen._compositor.render_strips()]


async def _open(pilot, screen):
    await pilot.app.push_screen(screen)
    await pilot.pause()
    return screen


# --- живой репозиторий: принятие ---------------------------------------------


@pytest.mark.asyncio
async def test_accept_flow_from_list_and_viewer(tmp_path):
    """`a` из списка и из вьюера: база сдвигается, файл уходит в группу
    «принято», git-индекс обновляется — принятие меняет данные, не только
    индекс (в отличие от старого git add, Р9)."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("first.py", "a\n", "a\nb\n", ts=2.0)
    ws.edit("second.py", "c\n", "c\nd\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert [e.path for e in scr._entries] == ["first.py", "second.py"]

        await pilot.press("a")                      # список: принять first.py
        await pilot.pause()

        assert ws.staged() == {"first.py"}
        assert f"принято: first.py — {ACCEPT_NEW_BASELINE}" in _flash_text(scr)
        assert [e.path for e in scr._entries] == ["second.py"]     # ушёл в группу
        assert ws.store.accepted_state("first.py") is not None
        assert any(row.path == "first.py" for row in scr._settled)

        await pilot.press("enter")                  # вьюер second.py
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)
        assert viewer.entry.path == "second.py"

        await pilot.press("a")                      # вьюер: принять second.py
        await pilot.pause()

        assert ws.staged() == {"first.py", "second.py"}
        assert pilot.app.screen is scr               # вьюер закрылся — файл ушёл из активных
        assert scr._entries == []
        assert ws.store.accepted_state("second.py") is not None
        assert any(row.path == "second.py" for row in scr._settled)
        assert f"принято: second.py — {ACCEPT_NEW_BASELINE}" in _flash_text(scr)


@pytest.mark.asyncio
async def test_a_follows_the_cursor(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("first.py", "a\n", "a\nb\n", ts=2.0)
    ws.edit("second.py", "c\n", "c\nd\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("down")
        await pilot.press("a")
        await pilot.pause()

        assert ws.staged() == {"second.py"}
        assert ws.store.accepted_state("first.py") is None
        assert ws.store.accepted_state("second.py") is not None


@pytest.mark.asyncio
async def test_ctrl_a_accepts_and_stages_the_whole_source(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("first.py", "a\n", "a\nb\n", ts=2.0)
    ws.edit("second.py", "c\n", "c\nd\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("ctrl+a")
        await pilot.pause()

        assert ws.staged() == {"first.py", "second.py"}
        assert scr._entries == []
        assert "принято: 2 файла" in _flash_text(scr)


@pytest.mark.asyncio
async def test_accept_works_outside_a_repository_too(tmp_path):
    """Р9-пересмотр: принятие доступно и без git — сдвигает базу, не стейджит.

    Раньше (git add) клавиша была НО-ОП вне репозитория (нечего ускорять).
    Теперь принятие не привязано к git вовсе: диск становится точкой отсчёта
    в любом случае, просто без индексного хвоста в плашке.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)                       # без .git → cache-mode
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert scr.mode == "cache-mode"

        await pilot.press("a")
        await pilot.pause()

        assert ws.store.accepted_state("a.py") is not None    # база сдвинулась
        assert scr._entries == []                              # ушёл в группу
        text = _flash_text(scr)
        assert text == f"✓ принято: a.py — {ACCEPT_NEW_BASELINE}"
        assert "индекс" not in text                            # без git-хвоста


@pytest.mark.asyncio
async def test_ack_mentions_the_new_baseline(tmp_path):
    """Живой акк, а не только чистая функция: текст обязан доехать до экрана."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()

        text = _flash_text(scr)
        assert ACCEPT_NEW_BASELINE in text
        assert text.startswith("✓ ")           # исход — успех, глиф канонный


@pytest.mark.asyncio
async def test_git_refusal_reaches_the_user_with_git_own_words(tmp_path):
    """Отказ стейджинга не откатывает уже сдвинутую базу (докстринг `accept`):
    файл всё равно принят, а плашка — жёлтая, с текстом git и с подсказкой,
    что делать."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.write(".gitignore", "ign.txt\n")
    ws.edit("ign.txt", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()

        assert ws.staged() == set()
        assert ws.store.accepted_state("ign.txt") is not None   # база всё равно сдвинута
        text = _flash_text(scr)
        assert text.startswith(f"⚠ принято: ign.txt, но {ACCEPT_STAGE_FAILED}")
        assert ACCEPT_STAGE_ACTION in text
        assert _flashes(scr)[0].has_class("set-flash-warn")


# --- плашка --------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_is_flash_widget_not_diff_status_line(tmp_path):
    """Акк — канонная плашка §16, не самодельная строка и не всплывашка."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        notes: list[str] = []
        pilot.app.notify = lambda text, **kw: notes.append(text)

        await pilot.press("a")
        await pilot.pause()

        assert not scr.query("#diff-status")
        flash = _flashes(scr)[0]
        assert flash.has_class("set-flash-ok")
        assert notes == []


@pytest.mark.asyncio
async def test_flash_stays_a_single_widget_across_repeated_accepts(tmp_path):
    """`remove()+mount()` в одном такте роняет DuplicateIds — плашка одна,
    сколько бы файлов подряд ни приняли (курсор наследует позицию: файл,
    принятый первым, уезжает в группу, и на его месте оказывается следующий)."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n", ts=2.0)
    ws.edit("b.py", "c\n", "c\nd\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()
        assert len(_flashes(scr)) == 1
        assert scr._entries and scr._entries[0].path == "b.py"

        await pilot.press("a")
        await pilot.pause()

        assert len(_flashes(scr)) == 1
        assert scr._entries == []
        assert "принято: b.py" in _flash_text(scr)


@pytest.mark.asyncio
async def test_flash_survives_list_reload(tmp_path):
    """Плашка живёт вне тела списка: перечитывание её не уносит."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()
        text = _flash_text(scr)

        scr._reload()
        await pilot.pause()
        assert _flash_text(scr) == text


@pytest.mark.asyncio
async def test_flash_swaps_outcome_class_in_place(tmp_path):
    """Успех после отказа не оставляет янтарную рамку от прошлого нажатия."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.write(".gitignore", "ign.txt\n")
    ws.edit("ign.txt", "a\n", "a\nb\n", ts=2.0)
    ws.edit("ok.py", "c\n", "c\nd\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")                    # ign.txt — отказ стейджинга
        await pilot.pause()
        assert _flashes(scr)[0].has_class("set-flash-warn")

        await pilot.press("a")                    # ign.txt ушёл в группу — ok.py под курсором
        await pilot.pause()
        flash = _flashes(scr)[0]
        assert flash.has_class("set-flash-ok")
        assert not flash.has_class("set-flash-warn")


@pytest.mark.asyncio
async def test_revert_ack_moved_from_notify_to_flash(tmp_path):
    """Хвост Задачи 9: итог отката — та же канонная плашка, а не всплывашка."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "исходное\n", "правленое\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        notes: list[str] = []
        pilot.app.notify = lambda text, **kw: notes.append(text)

        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert notes == []
        assert _flash_text(scr) == "✓ откат · восстановлено: a.py"
        assert ws.acks == ["откат · восстановлено: a.py"]   # строка в трейсе жива


@pytest.mark.asyncio
async def test_failed_revert_from_viewer_reaches_the_user_over_the_viewer(tmp_path):
    """Провалившийся откат из вьюера обязан быть виден — вьюер не закрывается.

    Живой промах: `changed=False` оставляет вьюер открытым, а плашка монтируется
    на списке ПОД ним — отказ пропадал молча. Гонка настоящая: снимок унесла
    конкурентная сессия между показом подтверждения и Enter.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "исходное\n", "правленое\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        notes: list[tuple[str, str]] = []
        pilot.app.notify = lambda text, **kw: notes.append((text, kw.get("severity")))

        await pilot.press("enter")
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)

        await pilot.press("r")
        await pilot.pause()
        next(snapshots_root(str(ws.root)).glob("*.orig")).unlink()   # снимок унесли
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is viewer          # ничего не изменилось — вьюер жив
        assert ws.read("a.py") == "правленое\n"
        assert len(notes) == 1
        text, severity = notes[0]
        assert text.startswith("откат не выполнен")
        assert severity == "warning"


@pytest.mark.asyncio
async def test_successful_revert_does_not_duplicate_the_ack(tmp_path):
    """Успех виден плашкой на списке — всплывашка была бы вторым голосом."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "исходное\n", "правленое\n", ts=2.0)
    ws.edit("b.py", "b\n", "b\nb2\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        notes: list[str] = []
        pilot.app.notify = lambda text, **kw: notes.append(text)

        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr             # вьюер закрылся сам
        assert notes == []
        assert _flash_text(scr) == "✓ откат · восстановлено: a.py"


@pytest.mark.asyncio
async def test_source_switch_drops_the_flash(tmp_path):
    """Плашка про прошлый набор над новым источником говорила бы не про него."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()
        assert _flashes(scr)

        await pilot.press("shift+tab")
        await pilot.pause()
        assert scr.source == "turn"
        assert _flashes(scr) == []

        # и возвращается по новому действию, а не остаётся мёртвой (a.py снова
        # активен в «Ходе» — там лестница и группа не действуют, Р27)
        await pilot.press("a")
        await pilot.pause()
        assert "принято: a.py" in _flash_text(scr)


@pytest.mark.asyncio
async def test_revert_flash_survives_the_reload_it_triggers(tmp_path):
    """Перечитывание после отката плашку НЕ снимает — она про него и есть."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "исходное\n", "правленое\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

        assert _flash_text(scr) == "✓ откат · восстановлено: a.py"


@pytest.mark.asyncio
async def test_flash_text_is_selectable_by_mouse(tmp_path):
    """Текст отказа копируют мышью — плашка обычный Static, как в /settings."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()

        assert _flashes(scr)[0].ALLOW_SELECT is True


# --- вьюер (Р20) --------------------------------------------------------------


@pytest.mark.asyncio
async def test_viewer_accept_works_outside_a_repository(tmp_path):
    """Не-git проект: принятие из вьюера тоже не требует git (Р9-пересмотр) —
    база сдвигается, вьюер закрывается вместе с уходом файла из активных."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)                       # без .git → cache-mode
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()
        assert "a принять" in _plain(pilot.app.screen.query_one("#footer"))

        await pilot.press("a")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert scr._entries == []
        assert ws.store.accepted_state("a.py") is not None


@pytest.mark.asyncio
async def test_failed_accept_from_viewer_reaches_the_user_via_notify(tmp_path):
    """Отказ принятия (файл сверх лимита) обязан быть виден — вьюер не
    закрывается (ничего не изменилось), а плашка списка под ним не видна: тот
    же промах, что ловило ревью отката (Р20), — принятие наследует ту же
    архитектуру (флеш на экране-списке, `app.notify` — резервный канал)."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\n" + "b" * 50 + "\n")

    config = dict(ws.config)
    config["agent_edits"] = {"max_diff_source_bytes": 1}

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=config
    ).run_test() as pilot:
        scr = DiffScreen(
            launch_root=str(ws.root), config=config,
            threads_dir=str(ws.threads_dir), system_id="cli", thread_id="th1",
            on_ui_ack=ws.acks.append,
        )
        await pilot.app.push_screen(scr)
        await pilot.pause()
        notes: list[tuple[str, str]] = []
        pilot.app.notify = lambda text, **kw: notes.append((text, kw.get("severity")))

        await pilot.press("enter")
        await pilot.pause()
        viewer = pilot.app.screen
        assert isinstance(viewer, DiffFileViewer)

        await pilot.press("a")
        await pilot.pause()

        assert pilot.app.screen is viewer           # ничего не изменилось — вьюер жив
        assert ws.store.accepted_state("a.py") is None
        assert len(notes) == 1
        text, severity = notes[0]
        assert "файл больше лимита" in text
        assert severity == "warning"


@pytest.mark.asyncio
async def test_viewer_footer_offers_accept_in_a_repo(tmp_path):
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()

        footer = _plain(pilot.app.screen.query_one("#footer"))
        assert "a принять" in footer
        assert "Ctrl+A" not in footer and "Ctrl+R" not in footer


@pytest.mark.asyncio
async def test_ctrl_a_is_not_a_viewer_key(tmp_path):
    """Р20: массовые действия живут там, где виден весь набор, — в списке."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("first.py", "a\n", "a\nb\n", ts=2.0)
    ws.edit("second.py", "c\n", "c\nd\n", ts=1.0, call_id="c2")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()
        viewer = pilot.app.screen

        await pilot.press("ctrl+a")
        await pilot.pause()

        assert ws.staged() == set()
        assert ws.store.accepted_state("first.py") is None
        assert _flashes(viewer) == []


@pytest.mark.asyncio
async def test_second_a_accepts_the_file_anew(tmp_path):
    """Повторное `a` (по возвращении к файлу с новыми правками поверх принятого)
    сдвигает базу заново — индекс отражает ПОСЛЕДНЕЕ содержимое."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n")

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("a")
        await pilot.pause()
        assert scr._entries == []

        ws.write("a.py", "a\nb\nc\n")
        ws.journal.append(_rec("a.py", ts=2.0, call_id="c2", add=1, del_=0))
        again = await _open(pilot, ws.screen())
        assert again._entries and again._entries[0].path == "a.py"    # снова активен

        await pilot.press("a")
        await pilot.pause()

        assert "принято: a.py" in _flash_text(again)
        # индекс и правда обновлён заново
        out = subprocess.run(
            ["git", "show", ":a.py"], cwd=str(ws.root), capture_output=True, check=True
        )
        assert out.stdout.decode("utf-8") == "a\nb\nc\n"


@pytest.mark.asyncio
async def test_empty_source_ignores_accept(tmp_path):
    """Пустой источник: принимать нечего, и плашки об этом тоже нет."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert not scr._entries

        await pilot.press("a")
        await pilot.press("ctrl+a")
        await pilot.pause()

        assert _flashes(scr) == []


# --- живой репозиторий: отмена принятия ---------------------------------------


@pytest.mark.asyncio
async def test_reset_confirm_only_when_delta_nonempty(tmp_path):
    """`u`: подтверждение просит, только когда дельта от принятой базы непуста.

    Пустая дельта — диск равен принятому, повторное `a` восстановило бы то же
    состояние — сброс происходит сразу. Непустая — полноэкранное подтверждение
    в жанре отката (задача 4 доп., С7): называет файл и цель, куда сместится
    счёт; отказ по Esc ничего не меняет.
    """
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("clean.py", "v1\n")
    ws.journal.append(_rec("clean.py", ts=2.0))
    ws.accept("clean.py", "v1\n")                    # диск == принятое → в группе
    ws.write("dirty.py", "v1\n")
    ws.journal.append(_rec("dirty.py", ts=1.0, call_id="c2"))
    ws.accept("dirty.py", "v1\n")
    ws.write("dirty.py", "v2\n")                      # дописано ПОСЛЕ принятия

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert [e.path for e in scr._entries] == ["dirty.py"]
        row = scr._view.active[0]
        assert row.step is not None and row.step.kind == STEP_ACCEPTED

        # dirty.py: дельта непуста — полноэкранное подтверждение, не модалка
        await pilot.press("u")
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        assert _plain(confirm.query_one("#header-left")) == "Сброс принятия · dirty.py"
        assert "после сброса счёт пойдёт от начала сессии" in _body(confirm)

        await pilot.press("escape")                   # отмена — база остаётся
        await pilot.pause()
        assert pilot.app.screen is scr
        assert ws.store.accepted_state("dirty.py") is not None

        await pilot.press("u")
        await pilot.pause()
        await pilot.press("enter")                    # подтвердили
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("dirty.py") is None

        # clean.py (в группе): дельта пуста — сразу, без подтверждения
        await pilot.press("down")                      # строка группы
        await pilot.pause()
        await pilot.press("enter")                      # раскрыть
        await pilot.pause()
        await pilot.press("down")                        # settled-строка clean.py
        await pilot.pause()

        await pilot.press("u")
        await pilot.pause()

        assert pilot.app.screen is scr                    # без подтверждения
        assert ws.store.accepted_state("clean.py") is None


@pytest.mark.asyncio
async def test_ctrl_u_confirm_mentions_count(tmp_path):
    """`Ctrl+U`: подтверждение обязано называть число задетых файлов (§4.3) —
    родительным падежом после предлога («у 2 файлов», не «у 2 файла», задача
    4 доп.) — и перечислять их в теле."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    for name, ts in (("a.py", 2.0), ("b.py", 1.0)):
        ws.write(name, "v1\n")
        ws.journal.append(_rec(name, ts=ts, call_id=f"c_{name}"))
        ws.accept(name, "v1\n")
        ws.write(name, "v2\n")          # дописано после принятия — дельта непуста у обоих

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert len(scr._entries) == 2

        await pilot.press("ctrl+u")
        await pilot.pause()

        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        assert _plain(confirm.query_one("#header-left")) == "Сброс принятия у 2 файлов"
        body = _body(confirm)
        assert "a.py" in body and "b.py" in body

        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("a.py") is None
        assert ws.store.accepted_state("b.py") is None
        assert "принятие отменено: 2 файла" in _flash_text(scr)


@pytest.mark.asyncio
async def test_u_in_viewer_resets_the_open_file_and_closes_on_change(tmp_path):
    """`u` во вьюере — снятие принятия открытого файла; при реальном изменении
    вьюер закрывается (тот же приём, что у отката и принятия из вьюера)."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")
    ws.write("a.py", "v2\n")            # дельта от принятого непуста

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffFileViewer)

        await pilot.press("u")
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        assert _plain(confirm.query_one("#header-left")) == "Сброс принятия · a.py"
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("a.py") is None


@pytest.mark.asyncio
async def test_u_in_viewer_resets_the_open_file_from_the_turn_source_too(tmp_path):
    """Тот же сценарий, что выше, но открыт из «Хода» (финальный фикс волны 4,
    Р27-ревью): владелец на смоуке оставил `u` рабочим и там — вьюер получает
    тот же жест над открытым файлом независимо от источника, эффект просто
    виден в «Сессии». Регрессия батча: `can_reset` вьюера строился из
    действующей ступени (`_is_accepted_step`), а она в «Ходе» всегда `None`
    (Р27, С1) — клавиша молчала для любого файла, открытого из «Хода»."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path)
    ws.write("a.py", "v1\n")
    ws.journal.append(_rec("a.py", ts=1.0))
    ws.accept("a.py", "v1\n")
    ws.write("a.py", "v2\n")            # дельта от принятого непуста

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen(source=SOURCE_TURN))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DiffFileViewer)

        await pilot.press("u")
        await pilot.pause()
        confirm = pilot.app.screen
        assert isinstance(confirm, RevertConfirmScreen)
        assert _plain(confirm.query_one("#header-left")) == "Сброс принятия · a.py"
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.screen is scr
        assert ws.store.accepted_state("a.py") is None


# --- гейт живого прогона ------------------------------------------------------


@pytest.mark.asyncio
async def test_actions_rejected_while_running(tmp_path):
    """Живой прогон пишет те же файлы — все четыре действия отклоняются тем
    же гейтом, что откат."""
    from gc_tui.app import TuiApp

    ws = _Workspace(tmp_path, git=True)
    ws.edit("a.py", "a\n", "a\nb\n", ts=2.0)
    ws.write("accepted.py", "v1\n")
    ws.journal.append(_rec("accepted.py", ts=1.0, call_id="c2"))
    ws.accept("accepted.py", "v1\n")
    ws.write("accepted.py", "v2\n")     # дельта непуста — у `u`/`Ctrl+U` есть что снять

    async with TuiApp(
        threads_dir=str(ws.threads_dir), system="cli", config=ws.config
    ).run_test() as pilot:
        scr = await _open(pilot, ws.screen())
        assert [e.path for e in scr._entries] == ["a.py", "accepted.py"]
        pilot.app.active_runner = SimpleNamespace(is_running=lambda: True)
        notes: list[tuple[str, str]] = []
        pilot.app.notify = lambda text, **kw: notes.append((text, kw.get("severity")))

        await pilot.press("a")                  # a.py — курсор здесь по умолчанию
        await pilot.pause()
        await pilot.press("down")               # accepted.py — у него есть `u`
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        await pilot.press("ctrl+a")
        await pilot.pause()
        await pilot.press("ctrl+u")
        await pilot.pause()

        assert pilot.app.screen is scr                  # ни принятия, ни подтверждения
        assert notes == [(BUSY_ACTION, "warning")] * 4
        assert ws.staged() == set()
        assert ws.store.accepted_state("a.py") is None
        assert ws.store.accepted_state("accepted.py") is not None   # снять не дали
