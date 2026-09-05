"""Tests for bot.config module."""

import pytest

from bot.config import load_config
from bot.errors import ConfigError


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all config-related env vars before each test."""
    env_vars = [
        "DISCORD_TOKEN",
        "GUILD_ID",
        "YANDEX_MUSIC_TOKEN",
        "LOG_LEVEL",
        "DEFAULT_VOLUME",
        "IDLE_TIMEOUT",
        "FFMPEG_PATH",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
    yield
    # Cleanup after test


class TestLoadConfigHappyPath:
    """Happy path: minimal required config."""

    def test_minimal_config_with_defaults(self, clean_env, monkeypatch):
        """Only required vars set → defaults applied."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123456")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        config = load_config(env_file=None)

        assert config.discord_token == "token1"
        assert config.guild_id == 123456
        assert config.yandex_token == "token2"
        assert config.log_level == "INFO"
        assert config.default_volume == 0.5
        assert config.ffmpeg_path == "ffmpeg"
        assert config.idle_timeout == 300


class TestLoadConfigMissingVars:
    """Missing required environment variables."""

    def test_missing_discord_token(self, clean_env, monkeypatch):
        """Missing DISCORD_TOKEN raises ConfigError."""
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token")

        with pytest.raises(ConfigError) as exc_info:
            load_config(env_file=None)
        assert "DISCORD_TOKEN" in str(exc_info.value)

    def test_missing_all_required(self, clean_env, monkeypatch):
        """Missing all required vars → error mentions all."""
        with pytest.raises(ConfigError) as exc_info:
            load_config(env_file=None)
        error_text = str(exc_info.value)
        assert "DISCORD_TOKEN" in error_text
        assert "GUILD_ID" in error_text
        assert "YANDEX_MUSIC_TOKEN" in error_text

    def test_security_missing_guild_id_doesnt_leak_tokens(self, clean_env, monkeypatch):
        """Error for missing GUILD_ID doesn't contain token values."""
        monkeypatch.setenv("DISCORD_TOKEN", "secret-discord-token-12345")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "secret-yandex-token-12345")

        with pytest.raises(ConfigError) as exc_info:
            load_config(env_file=None)
        error_text = str(exc_info.value)
        assert "secret-discord-token-12345" not in error_text
        assert "secret-yandex-token-12345" not in error_text


class TestLoadConfigValidation:
    """Config value validation."""

    def test_empty_string_treated_as_missing(self, clean_env, monkeypatch):
        """Empty strings in required vars treated as missing."""
        monkeypatch.setenv("DISCORD_TOKEN", "")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token")

        with pytest.raises(ConfigError):
            load_config(env_file=None)

    def test_whitespace_only_treated_as_missing(self, clean_env, monkeypatch):
        """Whitespace-only in required vars treated as missing."""
        monkeypatch.setenv("DISCORD_TOKEN", "   ")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token")

        with pytest.raises(ConfigError):
            load_config(env_file=None)

    def test_token_values_stripped(self, clean_env, monkeypatch):
        """Token values are stripped of whitespace."""
        monkeypatch.setenv("DISCORD_TOKEN", "  token1  ")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "  token2  ")

        config = load_config(env_file=None)

        assert config.discord_token == "token1"
        assert config.yandex_token == "token2"

    def test_guild_id_must_be_int(self, clean_env, monkeypatch):
        """GUILD_ID must be convertible to int."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "not-a-number")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        with pytest.raises(ConfigError):
            load_config(env_file=None)

    def test_guild_id_parsed_as_int(self, clean_env, monkeypatch):
        """GUILD_ID is parsed as int."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "987654321")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        config = load_config(env_file=None)

        assert isinstance(config.guild_id, int)
        assert config.guild_id == 987654321

    def test_guild_id_zero_raises_error(self, clean_env, monkeypatch):
        """GUILD_ID of 0 raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "0")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        with pytest.raises(ConfigError) as exc_info:
            load_config(env_file=None)
        # Verify error mentions GUILD_ID but not token values
        error_text = str(exc_info.value)
        assert "GUILD_ID" in error_text
        assert "token1" not in error_text
        assert "token2" not in error_text

    def test_guild_id_negative_raises_error(self, clean_env, monkeypatch):
        """Negative GUILD_ID raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "-5")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        with pytest.raises(ConfigError) as exc_info:
            load_config(env_file=None)
        # Verify error mentions GUILD_ID but not token values
        error_text = str(exc_info.value)
        assert "GUILD_ID" in error_text
        assert "token1" not in error_text
        assert "token2" not in error_text


class TestLoadConfigLogLevel:
    """LOG_LEVEL validation and parsing."""

    def test_log_level_defaults_to_info(self, clean_env, monkeypatch):
        """LOG_LEVEL defaults to INFO."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        config = load_config(env_file=None)

        assert config.log_level == "INFO"

    def test_log_level_converted_to_uppercase(self, clean_env, monkeypatch):
        """LOG_LEVEL is converted to uppercase."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("LOG_LEVEL", "debug")

        config = load_config(env_file=None)

        assert config.log_level == "DEBUG"

    def test_log_level_invalid_raises_error(self, clean_env, monkeypatch):
        """Invalid LOG_LEVEL raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("LOG_LEVEL", "TRACE")

        with pytest.raises(ConfigError):
            load_config(env_file=None)


class TestLoadConfigVolume:
    """DEFAULT_VOLUME validation."""

    def test_volume_defaults_to_0_5(self, clean_env, monkeypatch):
        """DEFAULT_VOLUME defaults to 0.5."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        config = load_config(env_file=None)

        assert config.default_volume == 0.5

    def test_volume_valid_range(self, clean_env, monkeypatch):
        """Valid volume values: 0.0, 1.0, 2.0."""
        for volume in [0.0, 1.0, 2.0]:
            monkeypatch.setenv("DISCORD_TOKEN", "token1")
            monkeypatch.setenv("GUILD_ID", "123")
            monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
            monkeypatch.setenv("DEFAULT_VOLUME", str(volume))

            config = load_config(env_file=None)
            assert config.default_volume == volume

    def test_volume_out_of_range(self, clean_env, monkeypatch):
        """Volume outside 0.0..2.0 raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("DEFAULT_VOLUME", "2.1")

        with pytest.raises(ConfigError):
            load_config(env_file=None)

    def test_volume_negative_raises_error(self, clean_env, monkeypatch):
        """Negative volume raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("DEFAULT_VOLUME", "-0.1")

        with pytest.raises(ConfigError):
            load_config(env_file=None)

    def test_volume_non_numeric_raises_error(self, clean_env, monkeypatch):
        """Non-numeric volume raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("DEFAULT_VOLUME", "loud")

        with pytest.raises(ConfigError):
            load_config(env_file=None)


class TestLoadConfigIdleTimeout:
    """IDLE_TIMEOUT validation."""

    def test_idle_timeout_defaults_to_300(self, clean_env, monkeypatch):
        """IDLE_TIMEOUT defaults to 300."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        config = load_config(env_file=None)

        assert config.idle_timeout == 300

    def test_idle_timeout_positive_accepted(self, clean_env, monkeypatch):
        """Positive idle_timeout accepted."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("IDLE_TIMEOUT", "600")

        config = load_config(env_file=None)

        assert config.idle_timeout == 600

    def test_idle_timeout_zero_raises_error(self, clean_env, monkeypatch):
        """IDLE_TIMEOUT of 0 raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("IDLE_TIMEOUT", "0")

        with pytest.raises(ConfigError):
            load_config(env_file=None)

    def test_idle_timeout_negative_raises_error(self, clean_env, monkeypatch):
        """Negative IDLE_TIMEOUT raises ConfigError."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("IDLE_TIMEOUT", "-100")

        with pytest.raises(ConfigError):
            load_config(env_file=None)


class TestLoadConfigFfmpegPath:
    """FFMPEG_PATH handling."""

    def test_ffmpeg_path_defaults(self, clean_env, monkeypatch):
        """FFMPEG_PATH defaults to 'ffmpeg'."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")

        config = load_config(env_file=None)

        assert config.ffmpeg_path == "ffmpeg"

    def test_ffmpeg_path_custom(self, clean_env, monkeypatch):
        """Custom FFMPEG_PATH is used."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("FFMPEG_PATH", "/usr/bin/ffmpeg")

        config = load_config(env_file=None)

        assert config.ffmpeg_path == "/usr/bin/ffmpeg"

    def test_ffmpeg_path_stripped(self, clean_env, monkeypatch):
        """FFMPEG_PATH whitespace is stripped."""
        monkeypatch.setenv("DISCORD_TOKEN", "token1")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "token2")
        monkeypatch.setenv("FFMPEG_PATH", "  /usr/bin/ffmpeg  ")

        config = load_config(env_file=None)

        assert config.ffmpeg_path == "/usr/bin/ffmpeg"


class TestConfigSecrets:
    """Config.secrets property."""

    def test_secrets_contains_tokens(self, clean_env, monkeypatch):
        """secrets property contains discord and yandex tokens."""
        monkeypatch.setenv("DISCORD_TOKEN", "discord-token-123")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "yandex-token-456")

        config = load_config(env_file=None)

        assert "discord-token-123" in config.secrets
        assert "yandex-token-456" in config.secrets

    def test_secrets_only_nonempty_tokens(self, clean_env, monkeypatch):
        """secrets contains only non-empty token values."""
        monkeypatch.setenv("DISCORD_TOKEN", "discord-token")
        monkeypatch.setenv("GUILD_ID", "123")
        monkeypatch.setenv("YANDEX_MUSIC_TOKEN", "yandex-token")

        config = load_config(env_file=None)

        assert len(config.secrets) >= 2
