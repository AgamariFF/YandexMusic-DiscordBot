"""Тесты извлечения звука из аудиокадров — защита от возврата «треска в каждом кадре».

История, ради которой написаны эти тесты: звук доставали из кадра как
`bytes(frame.planes[0])`, то есть весь буфер плоскости целиком. Библиотека
выделяет его с запасом на выравнивание (кадр 320 отсчётов s16 моно — 640
байт данных в буфере на 768), и лишние байты вклинивались между кадрами
каждые 20 мс. Поток становился на 20% длиннее, звучал ровным громким
треском, а распознавание речи на нём давало одно слово из восьми.

Коварство ошибки в том, что на ОДНОМ большом кадре она почти незаметна —
хвост один на всю запись, — и проверка «одним куском» её пропускает.
Поэтому тесты ниже гоняют звук ПОКАДРОВО, как он и приходит из Discord.
"""

from __future__ import annotations

import array
import math
import struct

import av
import pytest

from bot.listening import _build_speech_resampler
from bot.nekto.audio import (
    DISCORD_FRAME_SAMPLES,
    DISCORD_SAMPLE_RATE,
    build_discord_resampler,
    frame_to_pcm,
)
from bot.speech import SAMPLE_RATE

PACKET_BYTES = DISCORD_FRAME_SAMPLES * 2 * 2  # 20 мс, стерео, 16 бит


def _sine_48k_stereo(seconds: float, freq: int = 440, amplitude: int = 12000) -> bytes:
    """Чистый синус в формате Discord — эталон, в котором заведомо нет щелчков."""
    count = int(DISCORD_SAMPLE_RATE * seconds)
    values: list[int] = []
    for n in range(count):
        sample = int(amplitude * math.sin(2 * math.pi * freq * n / DISCORD_SAMPLE_RATE))
        values.append(sample)
        values.append(sample)
    return struct.pack(f"<{len(values)}h", *values)


def _build_frame(pcm: bytes) -> av.AudioFrame:
    """Собирает кадр ровно так же, как это делает приём звука из Discord."""
    frame = av.AudioFrame(format="s16", layout="stereo", samples=len(pcm) // 4)
    frame.sample_rate = DISCORD_SAMPLE_RATE
    memoryview(frame.planes[0])[:] = pcm
    return frame


def _spikes(pcm: bytes, threshold: int = 8000) -> int:
    """Считает резкие скачки между соседними отсчётами — на слух это и есть треск."""
    values = array.array("h")
    values.frombytes(pcm[: len(pcm) // 2 * 2])
    return sum(1 for a, b in zip(values, values[1:], strict=False) if abs(a - b) > threshold)


def _pump(pcm: bytes, resampler: av.AudioResampler) -> bytes:
    """Прогоняет звук покадрово через ресемплер — так, как это происходит в боте."""
    out = bytearray()
    for offset in range(0, len(pcm) - PACKET_BYTES + 1, PACKET_BYTES):
        for resampled in resampler.resample(_build_frame(pcm[offset : offset + PACKET_BYTES])):
            out.extend(frame_to_pcm(resampled))
    return bytes(out)


class TestFrameToPcm:
    """Извлечение ровно объявленных отсчётов, без выравнивающего хвоста."""

    @pytest.mark.parametrize("samples", [1, 7, 100, 320, 480, 960])
    def test_returns_declared_size(self, samples):
        """Длина результата равна объявленному числу отсчётов, а не размеру буфера."""
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        assert len(frame_to_pcm(frame)) == samples * 2

    def test_stereo_accounts_for_channels(self):
        """Для стерео учитываются оба канала."""
        frame = av.AudioFrame(format="s16", layout="stereo", samples=320)
        assert len(frame_to_pcm(frame)) == 320 * 2 * 2

    def test_ignores_alignment_padding(self):
        """Выравнивающий хвост в результат не попадает.

        Именно этот хвост и создавал треск: библиотека выделяет буфер с
        запасом, и всё, что за объявленными отсчётами, — не звук.
        """
        resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        frames = resampler.resample(_build_frame(_sine_48k_stereo(0.02)))
        assert frames, "ресемплер обязан отдать хотя бы один кадр"
        frame = frames[0]
        buffer_size = frame.planes[0].buffer_size
        extracted = len(frame_to_pcm(frame))
        assert extracted == frame.samples * 2
        # Сам факт запаса — предпосылка ошибки; если его вдруг не станет,
        # тест всё равно останется верным, но проверять будет нечего.
        assert extracted <= buffer_size

    def test_content_matches_buffer_start(self):
        """Берётся именно начало буфера, а не произвольный его кусок."""
        frame = av.AudioFrame(format="s16", layout="mono", samples=8)
        payload = struct.pack("<8h", *range(1, 9))
        memoryview(frame.planes[0])[: len(payload)] = payload
        assert frame_to_pcm(frame) == payload


class TestResamplingPipeline:
    """Покадровый прогон звука — регрессия на «треск в каждом кадре»."""

    def test_length_is_preserved(self):
        """Длительность звука после ресемплинга совпадает с исходной.

        На сломанном извлечении поток раздувался примерно в 1.2 раза — это
        самый простой и надёжный признак возврата ошибки.
        """
        source = _sine_48k_stereo(1.0)
        out = _pump(source, _build_speech_resampler())
        seconds_in = len(source) / 4 / DISCORD_SAMPLE_RATE
        seconds_out = len(out) / 2 / SAMPLE_RATE
        assert 0.97 <= seconds_out / seconds_in <= 1.03

    def test_clean_signal_stays_clean(self):
        """Чистый синус не обрастает щелчками, пройдя покадровый ресемплинг.

        Ресемплинг сам по себе даёт единичные неточности на стыках, но
        сотни скачков означают вернувшийся дефект.
        """
        out = _pump(_sine_48k_stereo(1.0), _build_speech_resampler())
        assert _spikes(out) < 20

    def test_broken_extraction_would_be_caught(self):
        """Наивное извлечение всего буфера обязано отличаться от правильного.

        Тест закрепляет саму суть дефекта: если бы разницы не было, все
        остальные проверки в этом файле ничего бы не стоили.
        """
        source = _sine_48k_stereo(0.5)
        resampler = _build_speech_resampler()
        naive = bytearray()
        correct = bytearray()
        for offset in range(0, len(source) - PACKET_BYTES + 1, PACKET_BYTES):
            frame = _build_frame(source[offset : offset + PACKET_BYTES])
            for resampled in resampler.resample(frame):
                naive.extend(bytes(resampled.planes[0]))
                correct.extend(frame_to_pcm(resampled))
        assert len(naive) > len(correct)
        assert _spikes(bytes(naive)) > _spikes(bytes(correct))

    def test_discord_direction_is_also_clean(self):
        """Обратное направление (в формат Discord) тоже не трещит — путь чат-рулетки."""
        source = _sine_48k_stereo(0.5)
        out = _pump(source, build_discord_resampler())
        seconds_in = len(source) / 4 / DISCORD_SAMPLE_RATE
        seconds_out = len(out) / 4 / DISCORD_SAMPLE_RATE
        assert 0.97 <= seconds_out / seconds_in <= 1.03
        assert _spikes(out) < 20
