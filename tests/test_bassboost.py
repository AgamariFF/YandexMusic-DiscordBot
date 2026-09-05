"""Tests for bot.audio.bassboost module."""

import pytest

from bot.audio.bassboost import BassLevel, build_audio_filter


class TestBassLevel:
    """Tests for BassLevel enum."""

    def test_enum_values(self):
        """BassLevel values match expected strings."""
        assert BassLevel.OFF.value == "off"
        assert BassLevel.LOW.value == "low"
        assert BassLevel.MEDIUM.value == "medium"
        assert BassLevel.HIGH.value == "high"
        assert BassLevel.EXTREME.value == "extreme"

    def test_gain_db_values(self):
        """gain_db property returns correct values."""
        assert BassLevel.OFF.gain_db == 0.0
        assert BassLevel.LOW.gain_db == 4.0
        assert BassLevel.MEDIUM.gain_db == 8.0
        assert BassLevel.HIGH.gain_db == 12.0
        assert BassLevel.EXTREME.gain_db == 18.0

    def test_gain_db_monotonic(self):
        """gain_db increases from OFF to EXTREME."""
        levels = [BassLevel.OFF, BassLevel.LOW, BassLevel.MEDIUM, BassLevel.HIGH, BassLevel.EXTREME]
        gains = [level.gain_db for level in levels]
        for i in range(len(gains) - 1):
            assert gains[i] < gains[i + 1]

    def test_label_not_empty(self):
        """Each level has a non-empty label."""
        for level in BassLevel:
            assert level.label
            assert isinstance(level.label, str)
            assert len(level.label) > 0

    def test_parse_exact_match(self):
        """parse with exact match."""
        assert BassLevel.parse("off") == BassLevel.OFF
        assert BassLevel.parse("low") == BassLevel.LOW
        assert BassLevel.parse("medium") == BassLevel.MEDIUM
        assert BassLevel.parse("high") == BassLevel.HIGH
        assert BassLevel.parse("extreme") == BassLevel.EXTREME

    def test_parse_case_insensitive(self):
        """parse is case insensitive."""
        assert BassLevel.parse("OFF") == BassLevel.OFF
        assert BassLevel.parse("Off") == BassLevel.OFF
        assert BassLevel.parse("HIGH") == BassLevel.HIGH
        assert BassLevel.parse("High") == BassLevel.HIGH

    def test_parse_with_whitespace(self):
        """parse strips whitespace."""
        assert BassLevel.parse("  off  ") == BassLevel.OFF
        assert BassLevel.parse("\thigh\n") == BassLevel.HIGH
        assert BassLevel.parse("  MEDIUM  ") == BassLevel.MEDIUM

    def test_parse_invalid_raises_error(self):
        """parse with invalid value raises ValueError."""
        with pytest.raises(ValueError):
            BassLevel.parse("turbo")
        with pytest.raises(ValueError):
            BassLevel.parse("unknown")


class TestBuildAudioFilter:
    """Tests for build_audio_filter function."""

    def test_off_returns_none(self):
        """build_audio_filter(BassLevel.OFF) returns None."""
        result = build_audio_filter(BassLevel.OFF)
        assert result is None

    def test_non_off_returns_string(self):
        """build_audio_filter for non-OFF returns string."""
        for level in [BassLevel.LOW, BassLevel.MEDIUM, BassLevel.HIGH, BassLevel.EXTREME]:
            result = build_audio_filter(level)
            assert isinstance(result, str)
            assert len(result) > 0

    def test_filter_contains_bass(self):
        """Filter contains 'bass=g=' for non-OFF levels."""
        for level in [BassLevel.LOW, BassLevel.MEDIUM, BassLevel.HIGH, BassLevel.EXTREME]:
            result = build_audio_filter(level)
            assert "bass=g=" in result

    def test_filter_contains_alimiter(self):
        """Filter contains 'alimiter' (limiter to prevent clipping)."""
        for level in [BassLevel.LOW, BassLevel.MEDIUM, BassLevel.HIGH, BassLevel.EXTREME]:
            result = build_audio_filter(level)
            assert "alimiter" in result

    def test_filter_contains_gain_value(self):
        """Filter contains correct gain values for each level."""
        assert "bass=g=4" in build_audio_filter(BassLevel.LOW)
        assert "bass=g=8" in build_audio_filter(BassLevel.MEDIUM)
        assert "bass=g=12" in build_audio_filter(BassLevel.HIGH)
        assert "bass=g=18" in build_audio_filter(BassLevel.EXTREME)

    def test_filter_format(self):
        """Filter format includes both bass and limiter."""
        result = build_audio_filter(BassLevel.HIGH)
        assert "bass=g=" in result and "alimiter" in result
        assert "," in result  # Filters are chained
