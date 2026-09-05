"""Загрузка и валидация конфигурации бота из переменных окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from bot.errors import ConfigError

_ALLOWED_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_VOLUME = 0.5
_DEFAULT_FFMPEG_PATH = "ffmpeg"
_DEFAULT_IDLE_TIMEOUT = 300


@dataclass(frozen=True, slots=True)
class Config:
    """Проверенная конфигурация бота."""

    discord_token: str
    guild_id: int
    yandex_token: str
    log_level: str
    default_volume: float
    ffmpeg_path: str
    idle_timeout: int

    @property
    def secrets(self) -> tuple[str, ...]:
        """Непустые значения токенов — используются для маскирования в логах."""
        return tuple(value for value in (self.discord_token, self.yandex_token) if value)


def load_config(env_file: str | os.PathLike[str] | None = ".env") -> Config:
    """Загружает переменные окружения (опционально из .env-файла) и собирает Config."""
    if env_file is not None and os.path.isfile(env_file):
        load_dotenv(env_file, override=False)

    discord_token = os.environ.get("DISCORD_TOKEN", "").strip()
    guild_id_raw = os.environ.get("GUILD_ID", "").strip()
    yandex_token = os.environ.get("YANDEX_MUSIC_TOKEN", "").strip()

    missing = [
        name
        for name, value in (
            ("DISCORD_TOKEN", discord_token),
            ("GUILD_ID", guild_id_raw),
            ("YANDEX_MUSIC_TOKEN", yandex_token),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing)}")

    try:
        guild_id = int(guild_id_raw)
    except ValueError as exc:
        raise ConfigError("Переменная окружения GUILD_ID должна быть целым числом.") from exc
    if guild_id <= 0:
        raise ConfigError("Переменная окружения GUILD_ID должна быть положительным числом.")

    log_level = os.environ.get("LOG_LEVEL", "").strip().upper() or _DEFAULT_LOG_LEVEL
    if log_level not in _ALLOWED_LOG_LEVELS:
        raise ConfigError(
            "Переменная окружения LOG_LEVEL должна быть одной из: "
            + ", ".join(_ALLOWED_LOG_LEVELS)
        )

    volume_raw = os.environ.get("DEFAULT_VOLUME", "").strip()
    if not volume_raw:
        default_volume = _DEFAULT_VOLUME
    else:
        try:
            default_volume = float(volume_raw)
        except ValueError as exc:
            raise ConfigError("Переменная окружения DEFAULT_VOLUME должна быть числом.") from exc
    if not 0.0 <= default_volume <= 2.0:
        raise ConfigError(
            "Переменная окружения DEFAULT_VOLUME должна быть в диапазоне 0.0..2.0."
        )

    idle_timeout_raw = os.environ.get("IDLE_TIMEOUT", "").strip()
    if not idle_timeout_raw:
        idle_timeout = _DEFAULT_IDLE_TIMEOUT
    else:
        try:
            idle_timeout = int(idle_timeout_raw)
        except ValueError as exc:
            raise ConfigError(
                "Переменная окружения IDLE_TIMEOUT должна быть целым числом."
            ) from exc
    if idle_timeout <= 0:
        raise ConfigError("Переменная окружения IDLE_TIMEOUT должна быть положительным числом.")

    ffmpeg_path = os.environ.get("FFMPEG_PATH", "").strip() or _DEFAULT_FFMPEG_PATH

    return Config(
        discord_token=discord_token,
        guild_id=guild_id,
        yandex_token=yandex_token,
        log_level=log_level,
        default_volume=default_volume,
        ffmpeg_path=ffmpeg_path,
        idle_timeout=idle_timeout,
    )
