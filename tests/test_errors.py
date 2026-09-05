"""Tests for bot.errors module."""

import pytest

from bot.errors import (
    BotError,
    ConfigError,
    NotConnectedError,
    NothingPlayingError,
    NotInVoiceChannelError,
    TrackUnavailableError,
    VoiceConnectError,
    WaveUnavailableError,
    YandexAuthError,
)


class TestBotError:
    """Tests for BotError class."""

    def test_user_message_defaults_to_default_message(self):
        """BotError without user_message returns default_message."""
        exc = BotError("внутренняя деталь")
        assert exc.user_message == BotError.default_message
        assert "внутренняя деталь" not in exc.user_message

    def test_user_message_explicit(self):
        """BotError with explicit user_message returns it."""
        exc = BotError("деталь", user_message="Понятный текст")
        assert exc.user_message == "Понятный текст"

    def test_user_message_never_contains_internal_details(self):
        """user_message must not contain the internal message."""
        internal = "secret-database-error-details"
        exc = BotError(internal)
        assert internal not in exc.user_message
        assert exc.user_message == BotError.default_message


class TestBotErrorSubclasses:
    """Tests for BotError subclasses."""

    @pytest.mark.parametrize(
        "error_class",
        [
            ConfigError,
            NotInVoiceChannelError,
            NotConnectedError,
            VoiceConnectError,
            YandexAuthError,
            WaveUnavailableError,
            TrackUnavailableError,
            NothingPlayingError,
        ],
    )
    def test_subclass_is_bot_error(self, error_class):
        """Each subclass is an instance of BotError."""
        exc = error_class()
        assert isinstance(exc, BotError)

    @pytest.mark.parametrize(
        "error_class",
        [
            ConfigError,
            NotInVoiceChannelError,
            NotConnectedError,
            VoiceConnectError,
            YandexAuthError,
            WaveUnavailableError,
            TrackUnavailableError,
            NothingPlayingError,
        ],
    )
    def test_subclass_has_nonempty_default_message(self, error_class):
        """Each subclass has a non-empty default_message."""
        assert hasattr(error_class, "default_message")
        assert error_class.default_message
        assert isinstance(error_class.default_message, str)

    def test_subclass_default_messages_differ(self):
        """Each subclass default_message differs from BotError."""
        subclasses = [
            ConfigError,
            NotInVoiceChannelError,
            NotConnectedError,
            VoiceConnectError,
            YandexAuthError,
            WaveUnavailableError,
            TrackUnavailableError,
            NothingPlayingError,
        ]
        for error_class in subclasses:
            assert error_class.default_message != BotError.default_message
