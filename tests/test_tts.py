"""Тесты синтеза речи: подготовка текста, приведение формата и обработка отказов.

Настоящая модель синтеза (129 МБ) в тестах не грузится — её заменяет
заглушка. Проверяется то, что принадлежит нам: очистка текста, приведение
звука из формата модели (22050 Гц моно) в формат Discord (48 кГц стерео) и
превращение любых сбоев в доменную ошибку, с которой умеет работать
Discord-слой.
"""

from __future__ import annotations

import array
import asyncio
import math
import sys
import types
from unittest.mock import patch

import pytest

from bot.errors import SpeechSynthesisUnavailableError
from bot.nekto.audio import DISCORD_CHANNELS, DISCORD_SAMPLE_RATE, DISCORD_SAMPLE_WIDTH
from bot.tts import (
    MAX_TEXT_LENGTH,
    MODEL_SAMPLE_RATE,
    SPEECH_GAIN,
    TextToSpeech,
    _amplify_pcm,
    clean_text,
)


def _tone(seconds: float, freq: int = 220, amplitude: int = 9000) -> array.array:
    """Синус в формате модели: 16 бит, моно, `MODEL_SAMPLE_RATE`."""
    count = int(MODEL_SAMPLE_RATE * seconds)
    return array.array(
        "h",
        (
            int(amplitude * math.sin(2 * math.pi * freq * n / MODEL_SAMPLE_RATE))
            for n in range(count)
        ),
    )


class FakeSynth:
    """Заглушка синтеза: отдаёт заранее заданный звук и запоминает вызовы."""

    def __init__(self, audio: array.array | None = None) -> None:
        self.audio = audio if audio is not None else _tone(1.0)
        self.calls: list[tuple[str, int]] = []

    def synth_audio(self, text: str, speaker_id: int = 0):
        """Повторяет интерфейс настоящего синтеза: текст и номер голоса на вход."""
        self.calls.append((text, speaker_id))
        return self.audio


def _install_fake_vosk_tts(monkeypatch: pytest.MonkeyPatch, synth: object) -> dict[str, int]:
    """Подменяет пакет vosk_tts заглушкой и считает, сколько раз грузилась модель."""
    counters = {"loads": 0}

    def make_model(*args, **kwargs):
        counters["loads"] += 1
        return object()

    module = types.ModuleType("vosk_tts")
    module.Model = make_model
    module.Synth = lambda model: synth
    monkeypatch.setitem(sys.modules, "vosk_tts", module)
    return counters


class TestCleanText:
    """Подготовка произносимого текста."""

    def test_collapses_whitespace(self):
        """Лишние пробелы и переводы строк схлопываются в одиночные пробелы."""
        assert clean_text("  привет   всем\nэто\tбот  ") == "привет всем это бот"

    def test_truncates_to_limit(self):
        """Слишком длинный текст обрезается: бот не должен занимать канал надолго."""
        assert len(clean_text("а" * (MAX_TEXT_LENGTH * 2))) == MAX_TEXT_LENGTH

    def test_empty_stays_empty(self):
        """Пустой текст и текст из пробелов дают пустую строку."""
        assert clean_text("") == ""
        assert clean_text("   \n\t ") == ""


class TestSynthesize:
    """Синтез и приведение звука к формату Discord."""

    @pytest.mark.asyncio
    async def test_output_is_discord_format(self, monkeypatch):
        """Длительность сохраняется, а формат становится 48 кГц стерео.

        Ошибка в ресемплинге здесь означала бы речь не той скорости или
        не той высоты — самый заметный на слух дефект.
        """
        seconds = 1.0
        synth = FakeSynth(_tone(seconds))
        _install_fake_vosk_tts(monkeypatch, synth)

        pcm = await TextToSpeech().synthesize("привет")
        frame_size = DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
        produced = len(pcm) / frame_size / DISCORD_SAMPLE_RATE
        assert produced == pytest.approx(seconds, abs=0.02)

    @pytest.mark.asyncio
    async def test_duration_helper_matches(self, monkeypatch):
        """Вспомогательный расчёт длительности согласован с реальной длиной."""
        synth = FakeSynth(_tone(0.5))
        _install_fake_vosk_tts(monkeypatch, synth)
        pcm = await TextToSpeech().synthesize("привет")
        assert TextToSpeech.duration_seconds(pcm) == pytest.approx(0.5, abs=0.02)

    @pytest.mark.asyncio
    async def test_text_is_cleaned_before_synthesis(self, monkeypatch):
        """В синтез уходит уже очищенный текст, а не исходный."""
        synth = FakeSynth()
        _install_fake_vosk_tts(monkeypatch, synth)
        await TextToSpeech().synthesize("  привет   всем  ")
        assert synth.calls[0][0] == "привет всем"

    @pytest.mark.asyncio
    async def test_speaker_id_is_passed(self, monkeypatch):
        """Выбранный голос передаётся в синтез."""
        synth = FakeSynth()
        _install_fake_vosk_tts(monkeypatch, synth)
        await TextToSpeech(speaker_id=3).synthesize("привет")
        assert synth.calls[0][1] == 3

    @pytest.mark.asyncio
    async def test_empty_text_does_not_load_model(self, monkeypatch):
        """Пустой текст не грузит модель и не синтезирует — просто пустой результат."""
        synth = FakeSynth()
        counters = _install_fake_vosk_tts(monkeypatch, synth)
        assert await TextToSpeech().synthesize("   ") == b""
        assert counters["loads"] == 0
        assert synth.calls == []

    @pytest.mark.asyncio
    async def test_empty_audio_gives_empty_result(self, monkeypatch):
        """Синтез, вернувший пустой звук, не ломает вызывающего."""
        _install_fake_vosk_tts(monkeypatch, FakeSynth(array.array("h")))
        assert await TextToSpeech().synthesize("привет") == b""


class TestModelLoading:
    """Загрузка модели синтеза."""

    @pytest.mark.asyncio
    async def test_model_loads_once(self, monkeypatch):
        """Модель грузится один раз и переиспользуется для всех фраз."""
        counters = _install_fake_vosk_tts(monkeypatch, FakeSynth())
        tts = TextToSpeech()
        await tts.synthesize("первая")
        await tts.synthesize("вторая")
        assert counters["loads"] == 1

    @pytest.mark.asyncio
    async def test_concurrent_calls_load_once(self, monkeypatch):
        """Одновременные обращения не грузят модель несколько раз параллельно."""
        counters = _install_fake_vosk_tts(monkeypatch, FakeSynth())
        tts = TextToSpeech()
        await asyncio.gather(*(tts.synthesize("фраза") for _ in range(4)))
        assert counters["loads"] == 1

    @pytest.mark.asyncio
    async def test_is_ready_reflects_loading(self, monkeypatch):
        """Признак готовности выставляется только после успешной загрузки."""
        _install_fake_vosk_tts(monkeypatch, FakeSynth())
        tts = TextToSpeech()
        assert not tts.is_ready
        await tts.ensure_ready()
        assert tts.is_ready


class TestFailures:
    """Любой сбой становится доменной ошибкой с понятным текстом для человека."""

    @pytest.mark.asyncio
    async def test_missing_package(self, monkeypatch):
        """Без установленного пакета ошибка объясняет, что именно сделать."""
        monkeypatch.setitem(sys.modules, "vosk_tts", None)
        with pytest.raises(SpeechSynthesisUnavailableError) as exc:
            await TextToSpeech().ensure_ready()
        assert "vosk-tts" in exc.value.user_message

    @pytest.mark.asyncio
    async def test_model_load_failure(self, monkeypatch):
        """Сбой загрузки модели не выпускает наружу чужое исключение."""
        module = types.ModuleType("vosk_tts")

        def broken(*args, **kwargs):
            raise RuntimeError("модель не читается")

        module.Model = broken
        module.Synth = lambda model: None
        monkeypatch.setitem(sys.modules, "vosk_tts", module)

        with pytest.raises(SpeechSynthesisUnavailableError):
            await TextToSpeech().ensure_ready()

    @pytest.mark.asyncio
    async def test_synthesis_failure(self, monkeypatch):
        """Сбой самого синтеза тоже сводится к доменной ошибке."""

        class BrokenSynth:
            def synth_audio(self, text, speaker_id=0):
                raise ValueError("не получилось")

        _install_fake_vosk_tts(monkeypatch, BrokenSynth())
        with pytest.raises(SpeechSynthesisUnavailableError):
            await TextToSpeech().synthesize("привет")


class TestAmplifyPcm:
    """Усиление амплитуды моно-PCM: отсчёты, насыщение, граничные случаи."""

    def test_scales_samples(self):
        """Каждый отсчёт домножается на коэффициент усиления.

        Например при gain=1.5 отсчёт 1000 становится 1500, отсчёт -1000
        становится -1500. Проверяется базовое поведение без насыщения.
        """
        pcm = array.array("h", [1000, -1000, 500]).tobytes()
        result = _amplify_pcm(pcm, 1.5)
        samples = array.array("h")
        samples.frombytes(result)
        assert list(samples) == [1500, -1500, 750]

    def test_clamps_at_upper_bound(self):
        """Отсчёт, вышедший за верхнюю границу int16, обрезается до 32767.

        При gain=1.5 отсчёт 30000 даёт 32767 (а не переполнение).
        Это ключевой тест: переполнение вместо насыщения даёт грубый треск.
        """
        pcm = array.array("h", [30000]).tobytes()
        result = _amplify_pcm(pcm, 1.5)
        samples = array.array("h")
        samples.frombytes(result)
        assert samples[0] == 32767

    def test_clamps_at_lower_bound(self):
        """Отсчёт, вышедший за нижнюю границу int16, обрезается до -32768.

        При gain=1.5 отсчёт -30000 даёт -32768 (а не переполнение).
        Симметричная проверка верхней границы.
        """
        pcm = array.array("h", [-30000]).tobytes()
        result = _amplify_pcm(pcm, 1.5)
        samples = array.array("h")
        samples.frombytes(result)
        assert samples[0] == -32768

    def test_unit_gain_returns_input_unchanged(self):
        """При gain=1.0 функция возвращает входной bytes без изменений.

        Быстрый путь: вход не должен ни разу пройти по циклу усиления.
        Проверяется побайтовое равенство.
        """
        pcm = array.array("h", [1000, -500, 32767, -32768]).tobytes()
        result = _amplify_pcm(pcm, 1.0)
        assert result == pcm
        assert result is pcm

    def test_empty_input_gives_empty_output(self):
        """Пустой bytes на входе даёт пустой bytes на выходе."""
        result = _amplify_pcm(b"", 1.5)
        assert result == b""

    def test_odd_length_drops_trailing_byte(self):
        """Хвостовой непарный байт отбрасывается, результат чётной длины.

        Для s16 каждый отсчёт — 2 байта, поэтому нечётная длина —
        повреждённые данные. Функция должна отбросить хвост, а не упасть.
        """
        pcm = array.array("h", [1000, -500]).tobytes() + b"\x00"
        result = _amplify_pcm(pcm, 1.5)
        assert len(result) % 2 == 0
        samples = array.array("h")
        samples.frombytes(result)
        assert list(samples) == [1500, -750]

    def test_speech_gain_value(self):
        """Константа SPEECH_GAIN в модуле равна 2.25 (страховка от регрессии)."""
        assert SPEECH_GAIN == 2.25

    def test_speech_gain_stays_below_clipping_ceiling(self):
        """Усиление не должно превышать безопасный потолок по амплитуде синтеза.

        Замерено на живом синтезе: сырой моно-PCM модели пикует на 29–40%
        шкалы int16, то есть самые «громкие» фразы упираются в клиппинг
        около x2.5. Граница здесь — не догадка, а страховка: поднять
        громкость ещё можно, но выше этой отметки уже придётся заново
        мерить запас, а не крутить константу на глаз.
        """
        assert SPEECH_GAIN <= 2.5

    @pytest.mark.asyncio
    async def test_amplification_applied_during_synthesis(self, monkeypatch):
        """`_synthesize_sync` пропускает звук модели через усиление с `SPEECH_GAIN`.

        Проверка коэффициента стоит ПОСЛЕ синтеза, а не внутри подменённой
        функции: та выполняется в рабочем потоке (`asyncio.to_thread`), и
        поднятый там `AssertionError` был бы превращён `synthesize` в
        `SpeechSynthesisUnavailableError` — тест прошёл бы даже при
        подставленном мимо константы коэффициенте, то есть ровно при той
        регрессии, от которой он и поставлен.
        """
        synth = FakeSynth(_tone(0.1))
        _install_fake_vosk_tts(monkeypatch, synth)

        with patch("bot.tts._amplify_pcm", wraps=_amplify_pcm) as amplify:
            await TextToSpeech().synthesize("привет")

        assert amplify.call_count == 1
        assert amplify.call_args[0][1] == SPEECH_GAIN
