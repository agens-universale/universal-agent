"""VoiceInputFlow — state machine голосового ввода TUI.

idle → recording → transcribing → idle. Владеет recorder'ом и фоновым
transcribe-потоком; колбэки on_* маршалятся на UI-поток через ui_call
(App.call_from_thread). Зависимости инъектируются — тестируется без
микрофона, сети и Textual.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, Optional

from gc_tui.voice.errors import VoiceError

MIN_SECONDS = 0.5   # короче — случайное нажатие: молчаливая отмена

IDLE = "idle"
RECORDING = "recording"
TRANSCRIBING = "transcribing"


class VoiceInputFlow:
    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        recorder_factory: Callable[[], Any],
        transcribe: Callable[[bytes, Dict[str, Any]], str],
        ui_call: Callable[..., None],
        on_state: Callable[[str], None],
        on_text: Callable[[str], None],
        on_error: Callable[[str], None],
        clock: Callable[[], float] = time.monotonic,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._cfg = cfg
        self._recorder_factory = recorder_factory
        self._transcribe = transcribe
        self._ui_call = ui_call
        self._on_state = on_state
        self._on_text = on_text
        self._on_error = on_error
        self._clock = clock
        self._spawn = spawn or self._spawn_thread
        self.state = IDLE
        self._recorder: Any = None
        self._stt_started_at: float = 0.0
        self._band_peak: float = 0.0   # бегущий пик спектра для АРУ эквалайзера
        self._band_display: list = [0.0] * 7   # EMA-сглаженные высоты для плавности

    @staticmethod
    def _spawn_thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()

    @property
    def elapsed_seconds(self) -> float:
        """Секунды текущей стадии — для таймера пилюли."""
        if self.state == RECORDING and self._recorder is not None:
            return self._recorder.elapsed_seconds
        if self.state == TRANSCRIBING:
            return self._clock() - self._stt_started_at
        return 0.0

    @property
    def input_level(self) -> float:
        """Живой уровень сигнала микрофона 0..1 (только во время записи)."""
        if self.state == RECORDING and self._recorder is not None:
            return getattr(self._recorder, "level", 0.0)
        return 0.0

    @property
    def input_bands(self) -> tuple:
        """Готовые высоты столбцов эквалайзера 0..1 — БЕЗ побочных эффектов.

        Читать её можно сколько угодно раз за кадр (футер собирает пилюлю дважды:
        нарисовать и померить ширину). Шаг анимации делает `_advance_bands` ровно
        один раз за кадр из `tick`; раньше он жил прямо здесь, и два чтения за кадр
        крутили сглаживание и АРУ с двойной скоростью."""
        return tuple(self._band_display)

    def _advance_bands(self) -> None:
        """Шаг анимации эквалайзера: свежий спектр рекордера → высоты столбцов.

        Автоусиление: бегущий пик спектра с медленным затуханием (×0.975 на
        кадр) — самая громкая полоса всегда у потолка независимо от абсолютной
        громкости микрофона; пол пика 0.02 не даёт «взлетать» чистому шуму.
        Коэффициент пересчитан под кадр 0.08 с (было 0.95 на кадр 0.15 с): темп в
        секундах прежний, иначе АРУ вдвое быстрее задирала бы шум в паузах.
        Рекордер без спектра (фейки в тестах) — плоский фолбэк из уровня."""
        if self.state != RECORDING or self._recorder is None:
            self._band_peak = 0.0
            self._band_display = [0.0] * 7
            return
        # Пересчёт уровня и спектра — здесь, на UI-потоке: в звуковом колбэке у него
        # дедлайн, и просрочка стоила бы выброшенных сэмплов (voice/recorder.analyse).
        analyse = getattr(self._recorder, "analyse", None)
        if callable(analyse):
            analyse()
        bands = getattr(self._recorder, "band_levels", None)
        if bands is None:
            bands = (getattr(self._recorder, "level", 0.0),) * 7
        gated = tuple(b if b > 0.003 else 0.0 for b in bands)   # шумовой гейт
        peak_raw = max(gated)
        self._band_peak = max(peak_raw, self._band_peak * 0.975, 0.02)
        norm = [min(1.0, b / self._band_peak) for b in gated]
        # Спектр без дыр: формант «подсвечивает» соседние полосы (сглаживание),
        # а при живом сигнале вся лента дышит — каждой полосе минимум 0.01
        # (высота ⡀), чтобы эквалайзер выглядел полным, а не 1–2 столбцами.
        n = len(norm)
        smoothed = [
            max(norm[i],
                0.4 * (norm[i - 1] if i > 0 else 0.0),
                0.4 * (norm[i + 1] if i + 1 < n else 0.0))
            for i in range(n)
        ]
        if peak_raw > 0.003:
            smoothed = [max(v, 0.01) for v in smoothed]
        # Плавность: EMA с асимметрией — вверх быстро (отклик), вниз мягко
        # («стекание»); скачки высот проходят через промежуточные ступени.
        # Коэффициенты подняты (было 0.6/0.35): при них лента догоняла голос за 2-3
        # кадра и оседала около секунды — вместе с придерживанием пика в рекордере
        # (снято) это читалось как «эквалайзер отстаёт». Теперь ~1 кадр вверх и ~3 вниз.
        display = []
        for prev, target in zip(self._band_display, smoothed):
            alpha = 0.8 if target > prev else 0.55
            display.append(prev + alpha * (target - prev))
        self._band_display = display

    def toggle(self) -> None:
        """Ctrl+T: старт записи | стоп+распознать. В transcribing — игнор."""
        if self.state == IDLE:
            self._start()
        elif self.state == RECORDING:
            self.stop_and_transcribe()

    def _start(self) -> None:
        recorder = self._recorder_factory()
        try:
            recorder.start()
        except VoiceError as exc:
            self._on_error(str(exc))
            return
        self._recorder = recorder
        # Лента с прошлой записи (отменённой Esc — там сброса не было) иначе мелькнула
        # бы первым кадром новой: до первого tick рендер показывает уже накопленное.
        self._band_peak = 0.0
        self._band_display = [0.0] * 7
        self.state = RECORDING
        self._on_state(RECORDING)

    def cancel_recording(self) -> None:
        """Esc: выбросить запись без распознавания."""
        if self.state != RECORDING:
            return
        self._recorder.cancel()
        self._recorder = None
        self.state = IDLE
        self._on_state(IDLE)

    def tick(self) -> None:
        """Тик таймера экрана: шаг анимации эквалайзера + авто-стоп max_seconds.

        Ровно одна точка, где двигается состояние картинки: рендер её только читает."""
        self._advance_bands()
        if self.state == RECORDING \
                and self.elapsed_seconds >= self._cfg.get("max_seconds", 120):
            self.stop_and_transcribe()

    def stop_and_transcribe(self) -> None:
        if self.state != RECORDING:
            return
        if self._recorder.elapsed_seconds < MIN_SECONDS:
            self.cancel_recording()
            return
        pcm = self._recorder.stop()
        self._recorder = None
        self.state = TRANSCRIBING
        self._stt_started_at = self._clock()
        self._on_state(TRANSCRIBING)
        self._spawn(lambda: self._worker(pcm))

    def _worker(self, pcm: bytes) -> None:
        """Фоновый поток: HTTP-распознавание; результат — на UI-поток."""
        try:
            text = self._transcribe(pcm, self._cfg)
        except VoiceError as exc:
            self._ui_call(self._finish, None, str(exc))
        except Exception as exc:   # неожиданный сбой не должен вешать стадию
            self._ui_call(self._finish, None, f"голосовой ввод: {exc}")
        else:
            self._ui_call(self._finish, text, None)

    def _finish(self, text: Optional[str], error: Optional[str]) -> None:
        self.state = IDLE
        if error is not None:
            self._on_error(error)
        elif text:
            self._on_text(text)
        self._on_state(IDLE)
