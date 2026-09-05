"""Tests for bot.cogs.music helpers."""

from bot.cogs.music import format_duration


class TestFormatDuration:
    """Tests for format_duration function."""

    def test_zero_seconds(self):
        """format_duration(0) returns '0:00'."""
        assert format_duration(0) == "0:00"

    def test_less_than_minute(self):
        """format_duration for seconds < 60 returns M:SS."""
        assert format_duration(1) == "0:01"
        assert format_duration(30) == "0:30"
        assert format_duration(59) == "0:59"

    def test_one_minute_five_seconds(self):
        """format_duration(65) returns '1:05'."""
        assert format_duration(65) == "1:05"

    def test_one_hour_two_minutes_five_seconds(self):
        """format_duration(3725) returns '1:02:05'."""
        assert format_duration(3725) == "1:02:05"

    def test_multiple_hours(self):
        """format_duration for hours >= 1 returns H:MM:SS."""
        # 2 hours 30 minutes 45 seconds = 2*3600 + 30*60 + 45 = 9045
        assert format_duration(9045) == "2:30:45"

    def test_float_seconds_truncated(self):
        """format_duration(59.9) doesn't raise and returns string."""
        result = format_duration(59.9)
        assert isinstance(result, str)
        # Should be "0:59" (truncated to int)
        assert ":" in result

    def test_negative_seconds(self):
        """format_duration with negative seconds doesn't raise."""
        result = format_duration(-10)
        assert isinstance(result, str)
        assert ":" in result

    def test_large_numbers(self):
        """format_duration with large durations works."""
        # 10 hours = 36000 seconds
        result = format_duration(36000)
        assert "10:" in result

    def test_returns_string(self):
        """format_duration always returns string."""
        for seconds in [0, 30, 65, 3725, -1, 59.9]:
            result = format_duration(seconds)
            assert isinstance(result, str)

    def test_format_with_leading_zeros_seconds(self):
        """Seconds are zero-padded to 2 digits."""
        assert format_duration(61) == "1:01"
        assert format_duration(605) == "10:05"

    def test_format_minutes_in_hours(self):
        """Minutes are zero-padded when hours present."""
        # 1 hour 5 minutes 0 seconds
        assert format_duration(3900) == "1:05:00"
