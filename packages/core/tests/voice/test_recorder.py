"""MicRecorder на fake-стриме: без микрофона и без sounddevice."""
import pytest

from gc_core.voice.errors import VoiceError
from gc_core.voice.recorder import MicRecorder


class FakeStream:
    def __init__(self, callback):
        self.callback = callback
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


def _factory(box):
    def factory(cb):
        box["stream"] = FakeStream(cb)
        return box["stream"]
    return factory


def test_start_stop_collects_pcm():
    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    assert rec.is_active
    box["stream"].callback(b"\x01\x02", 1, None, None)
    box["stream"].callback(b"\x03\x04", 1, None, None)
    assert rec.stop() == b"\x01\x02\x03\x04"
    assert not rec.is_active
    assert box["stream"].closed


def test_cancel_discards_audio():
    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    box["stream"].callback(b"\xff\xff", 1, None, None)
    rec.cancel()
    assert rec.stop() == b""


def test_elapsed_seconds_uses_clock():
    now = {"t": 100.0}
    rec = MicRecorder(stream_factory=lambda cb: FakeStream(cb), clock=lambda: now["t"])
    rec.start()
    now["t"] = 107.3
    assert rec.elapsed_seconds == pytest.approx(7.3)


def test_start_failure_wraps_voice_error():
    def broken_factory(cb):
        raise RuntimeError("no device")

    rec = MicRecorder(stream_factory=broken_factory)
    with pytest.raises(VoiceError):
        rec.start()


def test_start_failure_in_stream_start_leaves_inactive():
    class FailingStream:
        def __init__(self, callback):
            self.closed = False

        def start(self):
            raise RuntimeError("device busy")

        def stop(self):
            pass

        def close(self):
            self.closed = True

    box = {}

    def factory(cb):
        box["s"] = FailingStream(cb)
        return box["s"]

    rec = MicRecorder(stream_factory=factory)
    with pytest.raises(VoiceError):
        rec.start()
    assert not rec.is_active           # стрим очищен, не утёк
    assert box["s"].closed             # закрыт best-effort


def test_level_rises_on_signal_and_decays_in_silence():
    import array

    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    assert rec.level == 0.0
    loud = array.array("h", [8192] * 160).tobytes()   # RMS = 0.25 от максимума
    box["stream"].callback(loud, 160, None, None)
    rec.analyse()
    first = rec.level
    assert first == pytest.approx(0.25, abs=0.01)
    box["stream"].callback(b"\x00\x00" * 160, 160, None, None)
    rec.analyse()
    assert rec.level == 0.0        # тишина — метр падает сразу, придержки больше нет


def test_band_levels_follow_signal_frequency():
    import array
    import math

    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    assert rec.band_levels == (0.0,) * 7
    # чистый синус 1200 Гц — должен зажечь ровно «свою» полосу (индекс 3)
    sine = array.array("h", [int(20000 * math.sin(2 * math.pi * 1200 * n / 16000))
                             for n in range(512)]).tobytes()
    box["stream"].callback(sine, 512, None, None)
    rec.analyse()
    bands = rec.band_levels
    assert bands[3] == max(bands)
    assert bands[3] > 0.3
    box["stream"].callback(b"\x00\x00" * 512, 512, None, None)
    rec.analyse()
    assert rec.band_levels[3] < bands[3]     # тишина — полоса затухает


def _harmonic_comb(f0: float, amp: int = 9000, n: int = 512) -> bytes:
    """Голос — гребёнка гармоник с шагом в основной тон, а не чистый тон."""
    import array
    import math

    harmonics = [f0 * k for k in range(1, int(6500 / f0) + 1)]
    out = array.array("h")
    for i in range(n):
        t = i / 16000
        v = sum(math.sin(2 * math.pi * f * t) for f in harmonics) / len(harmonics)
        out.append(int(max(-32000, min(32000, amp * v))))
    return out.tobytes()


def test_bands_hear_the_whole_band_not_one_frequency():
    """Полоса обязана слышать свой диапазон целиком, а не одну частоту в центре.

    Раньше каждая полоса меряLась одним зондом Гёрцеля с окном ~31 Гц: попадёт в него
    гармоника или нет, зависело от основного тона говорящего и уплывало от интонации.
    Замер стендом (2026-08-06): на голосе 190 Гц середина ленты проваливалась ниже
    шумового гейта — эквалайзер стоял пустым, пока человек говорил."""
    gate = 0.003        # шумовой гейт тракта (VoiceInputFlow._advance_bands)
    per_pitch = {}
    for f0 in (110, 150, 190, 240):
        box = {}
        rec = MicRecorder(stream_factory=_factory(box))
        rec.start()
        box["stream"].callback(_harmonic_comb(f0), 512, None, None)
        rec.analyse()
        per_pitch[f0] = rec.band_levels[:5]     # 150…2000 Гц — тело голоса

    for f0, bands in per_pitch.items():
        assert all(b > gate for b in bands), \
            f"основной тон {f0} Гц: полосы ниже гейта — {bands}"
    # Главное свойство: высота голоса на показания почти не влияет. Со старым
    # одночастотным зондом разброс тут был кратным — полоса то ловила гармонику, то нет.
    for i in range(5):
        values = [per_pitch[f0][i] for f0 in per_pitch]
        assert max(values) < 3 * min(values), \
            f"полоса {i} пляшет от основного тона: {values}"


def test_band_mapping_follows_frequency():
    """Каждая полоса откликается на СВОЙ участок спектра — иначе это не эквалайзер."""
    import array
    import math

    for index, freq in ((0, 150), (3, 1200), (6, 6000)):
        box = {}
        rec = MicRecorder(stream_factory=_factory(box))
        rec.start()
        tone = array.array("h", [int(15000 * math.sin(2 * math.pi * freq * n / 16000))
                                 for n in range(512)]).tobytes()
        box["stream"].callback(tone, 512, None, None)
        rec.analyse()
        bands = rec.band_levels
        assert bands[index] == max(bands), f"{freq} Гц зажёг не свою полосу: {bands}"


def test_audio_callback_does_no_analysis():
    """Инвариант: в потоке PortAudio считать нечего — только сложить байты. Иначе
    просрочка дедлайна колбэка стоит выброшенных сэмплов, а выбрасывается ровно то,
    что уйдёт на распознавание."""
    import array

    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    loud = array.array("h", [8192] * 512).tobytes()
    box["stream"].callback(loud, 512, None, None)
    assert rec.level == 0.0                  # колбэк не считал
    assert rec.band_levels == (0.0,) * 7
    rec.analyse()
    assert rec.level > 0.2                   # посчитал UI-тик
    assert rec.stop() == loud                # звук при этом сохранён целиком


def test_bands_decay_without_new_audio():
    """Кадр без нового блока (стрим встал) — полосы тают, а не залипают."""
    import array

    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    box["stream"].callback(array.array("h", [8192] * 512).tobytes(), 512, None, None)
    rec.analyse()
    loud = rec.level
    rec.analyse()                            # звука не пришло
    assert rec.level == pytest.approx(loud * 0.5)


def test_analyse_takes_loudest_block_of_the_frame():
    """За кадр приходит несколько блоков, и пик речи может быть в любом. Берём самый
    громкий: по последнему картинка проваливалась бы на паузе между словами."""
    import array

    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    loud = array.array("h", [8192] * 512).tobytes()
    quiet = array.array("h", [80] * 512).tobytes()
    box["stream"].callback(quiet, 512, None, None)
    box["stream"].callback(loud, 512, None, None)     # пик в середине кадра
    box["stream"].callback(quiet, 512, None, None)
    rec.analyse()
    assert rec.level == pytest.approx(0.25, abs=0.01)  # уровень громкого, не последнего


def test_stream_asks_for_small_blocks():
    """Порция задана явно: с дефолтной («high latency», ~180 мс) звук приходил реже,
    чем перерисовывается пилюля, и эквалайзер отставал на глаз."""
    from gc_core.voice import recorder as rec_mod
    seen = {}

    class _FakeSD:
        @staticmethod
        def RawInputStream(**kwargs):
            seen.update(kwargs)
            return object()

    import sys
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = _FakeSD
    try:
        rec_mod._default_stream_factory(lambda *a: None)
    finally:
        if saved is not None:
            sys.modules["sounddevice"] = saved
        else:
            del sys.modules["sounddevice"]
    assert seen["blocksize"] == rec_mod.BLOCK_SAMPLES == 512
    assert seen["latency"] == "low"
    assert seen["samplerate"] == rec_mod.SAMPLE_RATE


def test_driver_overflow_is_counted_not_swallowed():
    """Драйвер сообщает о потере входного звука флагом status. Раньше его молча
    игнорировали, и обрезанное распознавание выглядело как «модель не расслышала»."""
    box = {}
    rec = MicRecorder(stream_factory=_factory(box))
    rec.start()
    assert rec.overflows == 0
    box["stream"].callback(b"\x00\x00" * 160, 160, None, "input overflow")
    assert rec.overflows == 1
