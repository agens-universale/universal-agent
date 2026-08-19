"""Выбор пути доставки текста в буфер: нативный или OSC 52, но не оба сразу.

Живой замер (2026-08-04, Windows Terminal): отправка OSC 52 заставляет терминал
самому лезть в системный буфер Windows. Буфер эксклюзивный, захват занимает у
терминала до 3-4 секунд, и всё это время он не рисует и не отдаёт события —
интерфейс выглядит зависшим, а наша параллельная нативная запись получает отказ
(в логах было от 10% до 80% отказов).

Отключение OSC 52 на локальной машине убрало и подвисания, и отказы: 19 записей,
0 отказов. Поэтому OSC 52 остаётся только там, где он единственный способ
доставить текст: SSH (нативная запись положила бы текст в буфер сервера) и
случай, когда нативная запись не удалась.
"""
from __future__ import annotations

import pytest

from gc_tui.services import clipboard_ops


class FakeApp:
    def __init__(self):
        self.osc_calls = []
        self._clipboard = ""

    def copy_to_clipboard(self, text):
        self.osc_calls.append(text)
        self._clipboard = text

    def notify(self, *args, **kwargs):
        pass


@pytest.fixture
def local_machine(monkeypatch):
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: False)


def test_native_success_does_not_send_osc52(local_machine, monkeypatch):
    """Главный случай: нативная запись удалась — терминал не трогаем."""
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: True)
    app = FakeApp()

    assert clipboard_ops.copy_out(app, "код", toast=True, blocking=True) is True
    assert app.osc_calls == []


def test_internal_buffer_filled_without_osc52(local_machine, monkeypatch):
    """Ctrl+V внутри TUI обязан работать и без отправки OSC 52."""
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: True)
    app = FakeApp()

    clipboard_ops.copy_out(app, "код", blocking=True)
    assert app._clipboard == "код"


def test_osc52_used_when_native_fails(local_machine, monkeypatch):
    """Нативная запись не удалась — пробуем через терминал, текст важнее."""
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: False)
    app = FakeApp()

    clipboard_ops.copy_out(app, "код", toast=True, blocking=True)
    assert app.osc_calls == ["код"]


def test_ssh_session_uses_osc52(monkeypatch):
    """В SSH нативная запись положила бы текст в буфер сервера, а не ваш."""
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: True)
    native_calls = []
    monkeypatch.setattr(clipboard_ops, "write_clipboard",
                        lambda text: native_calls.append(text) or True)
    app = FakeApp()

    assert clipboard_ops.copy_out(app, "код", toast=True, blocking=True) is True
    assert app.osc_calls == ["код"]


def test_worker_path_also_skips_osc52_on_success(local_machine, monkeypatch):
    """Асинхронный путь (Ctrl+C в живом TUI) ведёт себя так же, как синхронный."""
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: True)

    class WorkerApp(FakeApp):
        def run_worker(self, work, **kwargs):
            work()

        def call_from_thread(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    app = WorkerApp()
    clipboard_ops.copy_out(app, "код", toast=True, blocking=False)
    assert app.osc_calls == []
    assert app._clipboard == "код"


def test_linux_keeps_osc52_alongside_native(monkeypatch):
    """На Linux OSC 52 шлём всегда — там он безвреден и нужен как страховка.

    Причина страховки: `xclip -loops 1` отдаёт содержимое ОДИН раз и выходит,
    после первой вставки буфер X11 пустеет (содержимое живёт в процессе-владельце,
    а не в системе). OSC 52 оставляет копию у терминала. Подвисаний, из-за которых
    OSC 52 убран на Windows, здесь нет: буфер X11/Wayland не эксклюзивный и
    пишущий никого не блокирует.
    """
    monkeypatch.setattr(clipboard_ops.sys, "platform", "linux")
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: False)
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: True)
    app = FakeApp()

    clipboard_ops.copy_out(app, "код", toast=True, blocking=True)
    assert app.osc_calls == ["код"]


def test_macos_skips_osc52_when_native_works(monkeypatch):
    """macOS: pbcopy надёжен, а Terminal.app OSC 52 не поддерживает вовсе."""
    monkeypatch.setattr(clipboard_ops.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: False)
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: True)
    app = FakeApp()

    clipboard_ops.copy_out(app, "код", toast=True, blocking=True)
    assert app.osc_calls == []


def test_windows_skips_osc52_when_native_works(monkeypatch):
    """Windows: ровно тот случай, ради которого всё затевалось."""
    monkeypatch.setattr(clipboard_ops.sys, "platform", "win32")
    monkeypatch.setattr(clipboard_ops, "is_ssh_session", lambda: False)
    monkeypatch.setattr(clipboard_ops, "write_clipboard", lambda text: True)
    app = FakeApp()

    clipboard_ops.copy_out(app, "код", toast=True, blocking=True)
    assert app.osc_calls == []
