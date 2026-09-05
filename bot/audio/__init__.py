"""Аудио-слой: бас-буст и построение источников звука для discord.py."""

from __future__ import annotations

from bot.audio.bassboost import BassLevel, build_audio_filter
from bot.audio.source import TrackedAudioSource, build_ffmpeg_options, create_source

__all__ = [
    "BassLevel",
    "TrackedAudioSource",
    "build_audio_filter",
    "build_ffmpeg_options",
    "create_source",
]
