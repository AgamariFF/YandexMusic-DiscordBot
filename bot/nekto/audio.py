"""Аудио-мосты между WebRTC-собеседником и Discord-слоем поверх этой сессии.

Discord работает с PCM 48 кГц, 16 бит, стерео, кадрами по 20 мс (960
сэмплов на канал) — оба моста ниже рассчитаны на то, что именно в таком
формате Discord-слой поставляет исходящие фреймы через `push_frame` и
ожидает получать входящие через `get_frame`. Сам модуль ресемплинг не
делает ни в одну, ни в другую сторону (см. `build_discord_resampler` —
необязательный помощник на случай, если формат потока собеседника
отличается от формата Discord).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from fractions import Fraction

import av
from aiortc.mediastreams import AudioStreamTrack

logger = logging.getLogger(__name__)

DISCORD_SAMPLE_RATE = 48000
DISCORD_CHANNELS = 2
DISCORD_SAMPLE_WIDTH = 2  # 16 бит на сэмпл
DISCORD_FRAME_DURATION_MS = 20
DISCORD_FRAME_SAMPLES = DISCORD_SAMPLE_RATE * DISCORD_FRAME_DURATION_MS // 1000  # 960

# Глубина очередей аудио-мостов в фреймах. При кадрах по 20 мс это около
# секунды буфера — достаточно, чтобы пережить короткую просадку сети или
# паузу у собеседника, не накапливая при этом растущую задержку разговора
# при долгой просадке (переполнение просто роняет самый старый фрейм).
_MAX_QUEUED_FRAMES = 50


def build_discord_resampler() -> av.AudioResampler:
    """Собирает `av.AudioResampler`, приводящий фреймы к формату Discord (s16/48кГц/стерео).

    Ни `OutgoingAudioTrack`, ни `IncomingAudioSink` ресемплинг не делают —
    это осознанно оставлено Discord-слою: именно он знает, действительно
    ли поток от собеседника отличается по формату от того, что ожидает
    голосовой канал Discord. Функция — просто готовая точка входа, чтобы не
    настраивать `av.AudioResampler` заново в каждом месте, где он может
    понадобиться.
    """
    return av.AudioResampler(format="s16", layout="stereo", rate=DISCORD_SAMPLE_RATE)


class OutgoingAudioTrack(AudioStreamTrack):
    """Исходящий аудиотрек WebRTC: отдаёт собеседнику фреймы, положенные через `push_frame`.

    `recv()` — то, что дёргает aiortc изнутри `RTCRtpSender` при отправке
    очередного пакета; Discord-слою он не нужен вовсе, наружу смотрит
    только `push_frame`.
    """

    kind = "audio"

    def __init__(self, *, sample_rate: int = DISCORD_SAMPLE_RATE) -> None:
        """Готовит пустую очередь исходящих фреймов."""
        super().__init__()
        self._sample_rate = sample_rate
        self._queue: asyncio.Queue[av.AudioFrame] = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)
        self._samples_sent = 0

    def push_frame(self, frame: av.AudioFrame) -> None:
        """Кладёт исходящий фрейм в очередь на отправку собеседнику.

        Не блокирующий и не async: Discord-слой вызывает это из своего
        цикла чтения голосового канала и не должен ждать, пока фрейм уедет
        по сети. При переполнении очереди (собеседник или сеть не успевают)
        отбрасывается самый старый накопленный фрейм — лучше короткий
        провал в звуке у собеседника, чем растущая задержка разговора.
        """
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(frame)

    async def recv(self) -> av.AudioFrame:
        """Отдаёт aiortc очередной фрейм, проставляя pts по числу уже отданных сэмплов."""
        frame = await self._queue.get()
        frame.pts = self._samples_sent
        frame.time_base = Fraction(1, self._sample_rate)
        self._samples_sent += frame.samples
        return frame


class IncomingAudioSink:
    """Очередь входящих аудиофреймов от собеседника, наполняемая обработчиком события `track`.

    Формат фреймов — тот, что реально пришёл через WebRTC (как правило,
    декодированный aiortc Opus), а не обязательно формат Discord — привести
    его при необходимости к PCM 48 кГц/16 бит/стерео (например, через
    `build_discord_resampler`) должен Discord-слой перед тем, как отдать
    фрейм в голосовой канал.
    """

    def __init__(self) -> None:
        """Готовит пустую очередь входящих фреймов."""
        self._queue: asyncio.Queue[av.AudioFrame] = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)

    def put_frame(self, frame: av.AudioFrame) -> None:
        """Кладёт входящий фрейм в очередь; при переполнении отбрасывает самый старый.

        Переполнение здесь означает, что Discord-слой не успевает читать
        `get_frame` достаточно быстро — отбрасываем старое, а не растим
        задержку разговора или память без предела.
        """
        try:
            self._queue.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self._queue.put_nowait(frame)

    async def get_frame(self) -> av.AudioFrame:
        """Забирает очередной входящий фрейм, ожидая, если очередь сейчас пуста."""
        return await self._queue.get()

    def empty(self) -> bool:
        """Признак того, что во входящей очереди сейчас нет фреймов."""
        return self._queue.empty()
