"""Построение источника аудио для discord.py на основе ffmpeg."""

from __future__ import annotations

import logging

import discord

from bot.audio.bassboost import BassLevel, build_audio_filter
from bot.errors import BotError

logger = logging.getLogger(__name__)

FFMPEG_BEFORE_OPTIONS = (
    "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel warning"
)
FRAME_SECONDS = 0.02


class TrackedAudioSource(discord.PCMVolumeTransformer):
    """Источник звука с регулировкой громкости и подсчётом воспроизведённого времени."""

    def __init__(
        self, original: discord.AudioSource, *, volume: float = 1.0, start_offset: float = 0.0
    ) -> None:
        """Оборачивает исходный источник, запоминая начальное смещение по времени."""
        super().__init__(original, volume=volume)
        self._start_offset = start_offset
        self._frames_read = 0

    @property
    def elapsed(self) -> float:
        """Время воспроизведения в секундах: стартовое смещение плюс выданные фреймы."""
        return self._start_offset + self._frames_read * FRAME_SECONDS

    def read(self) -> bytes:
        """Читает очередной фрейм, увеличивая счётчик только при непустых данных."""
        data = super().read()
        if data:
            self._frames_read += 1
        return data


def build_before_options(seek: float = 0.0) -> str:
    """Строит before-options ffmpeg; при seek > 0 добавляет -ss перед -i."""
    if seek > 0:
        return f"-ss {seek:.3f} {FFMPEG_BEFORE_OPTIONS}"
    return FFMPEG_BEFORE_OPTIONS


def build_ffmpeg_options(bass: BassLevel) -> str:
    """Строит options ffmpeg: отключение видео и опциональный фильтр бас-буста."""
    options = "-vn"
    audio_filter = build_audio_filter(bass)
    if audio_filter:
        options += f' -af "{audio_filter}"'
    return options


def create_source(
    url: str,
    *,
    volume: float = 1.0,
    bass: BassLevel = BassLevel.OFF,
    ffmpeg_path: str = "ffmpeg",
    seek: float = 0.0,
) -> TrackedAudioSource:
    """Создаёт отслеживаемый источник аудио из ссылки на поток через ffmpeg."""
    clamped_volume = max(0.0, min(2.0, volume))
    before_options = build_before_options(seek)
    options = build_ffmpeg_options(bass)
    try:
        original = discord.FFmpegPCMAudio(
            url, executable=ffmpeg_path, before_options=before_options, options=options
        )
    except FileNotFoundError as exc:
        raise BotError(
            f"Исполняемый файл ffmpeg не найден: {ffmpeg_path!r}",
            user_message="Не удалось воспроизвести аудио: ffmpeg не найден на сервере.",
        ) from exc

    logger.debug(
        "Создан источник аудио: seek=%.3f bass=%s volume=%.2f", seek, bass.value, clamped_volume
    )
    return TrackedAudioSource(original, volume=clamped_volume, start_offset=seek)
