"""Tests for bot.audio.source module."""

import discord
import pytest

from bot.audio.bassboost import BassLevel
from bot.audio.source import (
    FRAME_SECONDS,
    TrackedAudioSource,
    build_before_options,
    build_ffmpeg_options,
)


class FakeAudioSource(discord.AudioSource):
    """Fake audio source for testing."""

    def __init__(self, frames):
        """frames: list of bytes objects to return on each read()."""
        self.frames = list(frames)
        self.index = 0

    def is_opus(self):
        """Return False so discord.py treats it as PCM."""
        return False

    def read(self):
        """Return next frame or b'' when exhausted."""
        if self.index >= len(self.frames):
            return b""
        data = self.frames[self.index]
        self.index += 1
        return data


class TestBuildBeforeOptions:
    """Tests for build_before_options function."""

    def test_default_no_seek(self):
        """build_before_options() without seek."""
        result = build_before_options()
        assert "-reconnect" in result
        assert "-ss" not in result

    def test_zero_seek_no_ss(self):
        """build_before_options(0) doesn't include -ss."""
        result = build_before_options(0.0)
        assert "-reconnect" in result
        assert "-ss" not in result

    def test_positive_seek_includes_ss(self):
        """build_before_options with positive seek includes -ss."""
        result = build_before_options(12.5)
        assert "-ss" in result
        assert "12.5" in result or "12.500" in result

    def test_seek_value_format(self):
        """Seek value is formatted correctly."""
        result = build_before_options(12.5)
        assert "-ss" in result
        # Value should appear after -ss
        idx = result.index("-ss")
        tail = result[idx:]
        assert any(str(x) in tail for x in ["12.5", "12.500", "12.50"])


class TestBuildFfmpegOptions:
    """Tests for build_ffmpeg_options function."""

    def test_always_contains_vn(self):
        """build_ffmpeg_options always contains -vn."""
        for level in BassLevel:
            result = build_ffmpeg_options(level)
            assert "-vn" in result

    def test_off_no_af(self):
        """build_ffmpeg_options(BassLevel.OFF) doesn't contain -af."""
        result = build_ffmpeg_options(BassLevel.OFF)
        assert "-af" not in result

    def test_non_off_contains_af(self):
        """build_ffmpeg_options for non-OFF contains -af."""
        for level in [BassLevel.LOW, BassLevel.MEDIUM, BassLevel.HIGH, BassLevel.EXTREME]:
            result = build_ffmpeg_options(level)
            assert "-af" in result


class TestTrackedAudioSource:
    """Tests for TrackedAudioSource class."""

    def test_inherits_pcm_volume_transformer(self):
        """TrackedAudioSource inherits discord.PCMVolumeTransformer."""
        fake = FakeAudioSource([b"\x00" * 3840])
        source = TrackedAudioSource(fake)
        assert isinstance(source, discord.PCMVolumeTransformer)

    def test_elapsed_without_reads(self):
        """elapsed == start_offset when no reads."""
        fake = FakeAudioSource([])
        source = TrackedAudioSource(fake, start_offset=0.0)
        assert source.elapsed == 0.0

    def test_elapsed_with_start_offset(self):
        """elapsed starts at start_offset."""
        fake = FakeAudioSource([])
        source = TrackedAudioSource(fake, start_offset=30.0)
        assert source.elapsed == 30.0

    def test_elapsed_after_successful_reads(self):
        """elapsed increases after successful read()."""
        frame = b"\x00" * 3840
        fake = FakeAudioSource([frame, frame, frame])
        source = TrackedAudioSource(fake)

        source.read()
        source.read()
        source.read()

        expected = 3 * FRAME_SECONDS
        assert source.elapsed == pytest.approx(expected)

    def test_elapsed_with_offset_and_reads(self):
        """elapsed = start_offset + frame_count * FRAME_SECONDS."""
        frame = b"\x00" * 3840
        fake = FakeAudioSource([frame, frame])
        source = TrackedAudioSource(fake, start_offset=10.0)

        source.read()
        source.read()

        expected = 10.0 + 2 * FRAME_SECONDS
        assert source.elapsed == pytest.approx(expected)

    def test_read_empty_doesnt_increment_frames(self):
        """read() returning b'' doesn't increment elapsed."""
        fake = FakeAudioSource([b"\x00" * 3840])
        source = TrackedAudioSource(fake)

        source.read()
        elapsed_after_data = source.elapsed

        source.read()  # This returns b''
        source.read()  # This also returns b''

        assert source.elapsed == elapsed_after_data

    def test_volume_property(self):
        """volume can be set and read."""
        fake = FakeAudioSource([])
        source = TrackedAudioSource(fake, volume=0.5)
        assert source.volume == 0.5

        source.volume = 0.75
        assert source.volume == 0.75

    def test_volume_clamping(self):
        """volume can be clamped to valid range."""
        fake = FakeAudioSource([])
        source = TrackedAudioSource(fake)

        # discord.PCMVolumeTransformer clamps volume
        source.volume = 0.5
        assert 0.0 <= source.volume <= 2.0

        source.volume = 2.0
        assert source.volume == 2.0
