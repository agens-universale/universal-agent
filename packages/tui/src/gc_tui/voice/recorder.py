"""Захват микрофона: sounddevice.RawInputStream → сырой PCM16 mono в памяти.

sounddevice ставится вместе с агентом (PortAudio бандлится в колесе на
Windows/macOS; Linux требует системный libportaudio2). Импорт ленивый:
при недоступном аудио-стеке TUI живёт, available() — тихая проверка.
"""
from __future__ import annotations

import array
import math
import time
from typing import Callable, Optional

from gc_tui.voice.errors import VoiceError

SAMPLE_RATE = 16000
# Порция звука от драйвера: 512 отсчётов = 32 мс. Меньше кадра UI (80 мс), чтобы в
# каждый кадр приходил свежий звук, и достаточно, чтобы Гёрцель считал по целому окну.
BLOCK_SAMPLES = 512
# Центры частотных полос спектр-эквалайзера (Гц): диапазон живой речи.
BAND_FREQS = (150, 350, 700, 1200, 2000, 3500, 6000)
_NYQUIST = SAMPLE_RATE / 2


def _band_edges(index: int) -> tuple[float, float]:
    """Границы полосы — геометрические середины до соседей: полосы стыкуются встык,
    без дыр между ними (дыра = частоты, которых эквалайзер не слышит вовсе)."""
    freq = BAND_FREQS[index]
    if index > 0:
        low = math.sqrt(freq * BAND_FREQS[index - 1])
    else:
        low = freq * freq / math.sqrt(freq * BAND_FREQS[1])
    if index + 1 < len(BAND_FREQS):
        high = math.sqrt(freq * BAND_FREQS[index + 1])
    else:
        high = min(_NYQUIST * 0.975, freq * freq / low)
    return low, high


def _bandpass_coeffs(index: int) -> tuple[float, float, float]:
    """Полосовой биквад (RBJ, единичное усиление в центре) → (b0, a1, a2).

    Раньше полоса меряLась ОДНИМ зондом Гёрцеля — окошком в ~31 Гц вокруг центра.
    Голос же гребёнка гармоник с шагом в основной тон (110 Гц у низкого голоса,
    190 у высокого): попала гармоника в окошко или нет — дело случая, и оно менялось
    на лету от интонации. Отсюда «столбики плещутся внизу и иногда прыгают», а на
    голосе повыше лента была почти пустой (замер стендом 2026-08-06). Фильтр слышит
    полосу целиком, поэтому уровень идёт за энергией голоса, а не за удачей.

    b1 = 0 и b2 = -b0 по построению полосового биквада, поэтому в разностном
    уравнении остаётся b0*(x[n] - x[n-2]) — их не храним."""
    freq = BAND_FREQS[index]
    low, high = _band_edges(index)
    q = freq / max(1.0, high - low)
    w0 = 2.0 * math.pi * freq / SAMPLE_RATE
    alpha = math.sin(w0) / (2.0 * q)
    a0 = 1.0 + alpha
    return alpha / a0, (-2.0 * math.cos(w0)) / a0, (1.0 - alpha) / a0


_BAND_FILTERS = tuple(_bandpass_coeffs(i) for i in range(len(BAND_FREQS)))


def available() -> bool:
    """Доступен ли аудио-стек (sounddevice импортируется без ошибок)."""
    try:
        import sounddevice  # noqa: F401  (ленивый импорт: стек может отсутствовать)
        return True
    except Exception:
        return False


def _default_stream_factory(callback):
    import sounddevice  # ленивый импорт: аудио-стек может отсутствовать (Linux без libportaudio2)
    # blocksize/latency заданы ЯВНО. По умолчанию sounddevice просит «high latency», и
    # PortAudio отдаёт звук порциями по ~180 мс — реже, чем перерисовывается пилюля:
    # эквалайзер отставал на глаз. Раньше мелкую порцию просить было нельзя (в колбэке
    # висел спектр, 32 мс он бы не уложился и драйвер терял бы сэмплы); теперь колбэк
    # только складывает байты, и мелкая порция ничего не стоит.
    return sounddevice.RawInputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback,
        blocksize=BLOCK_SAMPLES, latency="low",
    )


class MicRecorder:
    """Один цикл записи: start → stop (PCM-байты) | cancel. Не переиспользуется."""

    def __init__(self, stream_factory: Optional[Callable] = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._stream_factory = stream_factory or _default_stream_factory
        self._clock = clock
        self._chunks: list[bytes] = []
        self._stream = None
        self._started_at: Optional[float] = None
        self._level = 0.0
        self._band_levels = [0.0] * len(BAND_FREQS)
        self._pending: list[bytes] = []   # блоки, пришедшие с прошлого кадра
        # Состояние полосовых фильтров (x1, x2, y1, y2 на полосу) живёт МЕЖДУ блоками:
        # сброс на каждом блоке дал бы переходный процесс на каждые 32 мс.
        self._band_state = [[0.0, 0.0, 0.0, 0.0] for _ in BAND_FREQS]
        self._overflows = 0

    def _on_audio(self, indata, frames, time_info, status) -> None:
        # Поток PortAudio с ЖЁСТКИМ дедлайном на блок: не уложились — драйвер выбросит
        # следующий кусок звука, и он не попадёт в stop(), то есть в распознавание.
        # Симптом при этом один — «плохо расслышал», без единой ошибки. Поэтому здесь
        # только дешёвое: сложить байты (list.append атомарен под GIL) и запомнить
        # последний блок. Уровень и спектр считает analyse() из UI-тика, где дедлайна
        # нет: раньше 7 полос Гёрцеля по 512 отсчётов (тысячи операций чистого Python)
        # висели прямо в этом колбэке.
        data = bytes(indata)
        self._chunks.append(data)
        self._pending.append(data)
        if status:
            self._overflows += 1     # драйвер сообщил о потере звука — больше не молчим

    def analyse(self) -> None:
        """Пересчитать уровень и спектр по звуку, накопленному с прошлого кадра.

        Берём САМЫЙ ГРОМКИЙ из пришедших блоков, а не последний: за кадр их приходит
        несколько, и пик речи мог быть в любом. Поиск громкого — `max`/`min` по array,
        то есть C-цикл; дорогой Гёрцель считается один раз, по выбранному блоку.

        Ни уровень, ни полосы больше не «придерживаются» (было `max(новое, прошлое×0.8)`).
        Придерживание имело смысл, пока колбэк звали ЧАЩЕ кадра — оно ловило пик между
        кадрами. Теперь пик ловит выбор блока, а лишний слой только тянул хвост: вместе
        с EMA получалось два сглаживателя подряд, и эквалайзер заметно отставал от голоса.
        Сглаживание осталось одно — в UI (`VoiceInputFlow._advance_bands`)."""
        pending, self._pending = self._pending, []
        blocks = []
        for data in pending:
            samples = array.array("h", data[: len(data) // 2 * 2])
            if samples:
                blocks.append(samples)
        if not blocks:
            # Звука не пришло вовсе (стрим встал): гасим быстро, чтобы лента не висела.
            self._level *= 0.5
            self._band_levels = [b * 0.5 for b in self._band_levels]
            return
        level = 0.0
        bands = [0.0] * len(BAND_FREQS)
        for samples in blocks:
            step = max(1, len(samples) // 512)   # сэмплируем RMS для скорости
            picked = samples[::step]
            rms = (sum(s * s for s in picked) / len(picked)) ** 0.5 / 32768.0
            if rms > level:
                level = rms
            for i, (b0, a1, a2) in enumerate(_BAND_FILTERS):
                state = self._band_state[i]
                x1, x2, y1, y2 = state
                energy = 0.0
                for x in samples:
                    y = b0 * (x - x2) - a1 * y1 - a2 * y2
                    x2, x1 = x1, x
                    y2, y1 = y1, y
                    energy += y * y
                state[0], state[1], state[2], state[3] = x1, x2, y1, y2
                band_rms = (energy / len(samples)) ** 0.5 / 32768.0
                if band_rms > bands[i]:
                    bands[i] = band_rms
        # По блокам берём МАКСИМУМ, а не среднее: за кадр (80 мс) их приходит два-три,
        # и усреднение съело бы пик слога — лента получилась бы вялой.
        self._level = level
        self._band_levels = bands

    @property
    def overflows(self) -> int:
        """Сколько раз драйвер сообщил о потере входного звука за эту запись.
        Больше нуля — часть фразы до распознавания не доехала."""
        return self._overflows

    @property
    def level(self) -> float:
        """Сглаженный уровень входного сигнала 0..1 (0 — тишина)."""
        return min(1.0, self._level)

    @property
    def band_levels(self) -> tuple:
        """Спектр по полосам BAND_FREQS, каждая 0..1 (эквалайзер записи)."""
        return tuple(min(1.0, b) for b in self._band_levels)

    def start(self) -> None:
        try:
            self._stream = self._stream_factory(self._on_audio)
            self._stream.start()
        except ImportError as exc:
            self._close()
            raise VoiceError(
                "голосовой ввод: аудио-стек sounddevice недоступен "
                "(Linux: установите libportaudio2)"
            ) from exc
        except Exception as exc:
            self._close()
            raise VoiceError(f"микрофон недоступен: {exc}") from exc
        self._started_at = self._clock()

    @property
    def is_active(self) -> bool:
        return self._stream is not None

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

    def _close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass  # закрытие best-effort: устройство могло отвалиться

    def stop(self) -> bytes:
        """Остановить и вернуть записанный PCM16 (16kHz mono)."""
        self._close()
        return b"".join(self._chunks)

    def cancel(self) -> None:
        """Остановить и выбросить запись."""
        self._close()
        self._chunks.clear()
