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
_DEFAULT_SPEECH_MODEL_PATH = "models/vosk-model-small-ru-0.22"
# Предел отладочной записи речи на одного говорящего — см.
# bot.speech_debug.DEFAULT_MAX_SECONDS про то, почему предел вообще нужен.
_DEFAULT_SPEECH_DEBUG_MAX_SECONDS = 600.0
# Модель и голос синтеза речи — см. bot.tts про то, почему выбраны эти.
_DEFAULT_TTS_MODEL_NAME = "vosk-model-tts-ru-0.7-multi"
_DEFAULT_TTS_SPEAKER_ID = 2

# Значения переменных окружения, считающиеся истиной для булевых флагов —
# тот же набор, что и у VOICE_RECV_DIAG (см. bot.voice_dave.diagnostics_enabled),
# ради единообразия разбора булевых настроек по всему проекту.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


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
    nekto_token: str
    nekto_user_agent: str
    speech_model_path: str
    speech_enabled: bool
    speech_transcript: bool
    speech_debug_dir: str
    speech_debug_max_seconds: float
    tts_enabled: bool
    tts_model_name: str
    tts_speaker_id: int
    voice_replies: bool

    @property
    def secrets(self) -> tuple[str, ...]:
        """Непустые значения токенов — используются для маскирования в логах."""
        return tuple(
            value for value in (self.discord_token, self.yandex_token, self.nekto_token) if value
        )


def _parse_bool_env(name: str, *, default: bool) -> bool:
    """Разбирает булеву переменную окружения: 1/true/yes/on без учёта регистра — истина.

    Пустая или отсутствующая переменная — `default`; любое другое значение
    (`0`, `false`, `no`, опечатка) — ложь. Общий разбор для `SPEECH_ENABLED`
    и `SPEECH_TRANSCRIPT`, чтобы не дублировать один и тот же код разбора.
    """
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUE_VALUES


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

    # Оба поля необязательны: без них бот запускается как обычно, просто без
    # команд чат-рулетки (см. bot/__main__.py). Но если задано хоть одно —
    # обязаны быть оба: сервис nekto.me сверяет токен с тем самым браузерным
    # User-Agent, которым он был получен (подпись рукопожатия завязана на
    # оба значения разом, см. bot/nekto/protocol.py), и токен без "своего"
    # user-agent (или наоборот) не пройдёт рукопожатие ни при каких условиях.
    nekto_token = os.environ.get("NEKTO_TOKEN", "").strip()
    nekto_user_agent = os.environ.get("NEKTO_USER_AGENT", "").strip()
    if bool(nekto_token) != bool(nekto_user_agent):
        raise ConfigError(
            "NEKTO_TOKEN и NEKTO_USER_AGENT должны быть заданы вместе: токен чат-рулетки "
            "действителен только с User-Agent того же браузера, которым он был получен."
        )

    # Путь к модели Vosk для распознавания речи — как и FFMPEG_PATH, только
    # проверяется на формат (непустая строка), а не на реальное наличие на
    # диске: модель весит десятки мегабайт и не кладётся в репозиторий (см.
    # .gitignore), поэтому её отсутствие — обычное дело при первом запуске.
    # Ошибку в этом случае показывает сама команда распознавания, а не
    # загрузка конфигурации (см. bot.speech.SpeechRecognizer), чтобы бот всё
    # равно запускался и работал без этой функции.
    speech_model_path = (
        os.environ.get("SPEECH_MODEL_PATH", "").strip() or _DEFAULT_SPEECH_MODEL_PATH
    )

    # Распознавание речи включено по умолчанию — выключается явно, если
    # владелец сервера не хочет постоянного прослушивания голосовых каналов
    # (см. README, предупреждение о приватности в разделе 7).
    speech_enabled = _parse_bool_env("SPEECH_ENABLED", default=True)
    # А вот отладочная публикация каждой распознанной фразы в текстовый
    # канал — наоборот, выключена по умолчанию: не всем нужен такой поток
    # сообщений, и без явного включения это была бы скрытая утечка чужих
    # разговоров в чат.
    speech_transcript = _parse_bool_env("SPEECH_TRANSCRIPT", default=False)

    # Каталог отладочной записи речи (см. bot.speech_debug): пустое значение
    # — запись выключена, и это единственный разумный умолчательный режим.
    # Постоянно писать на диск чужую речь недопустимо, поэтому включается
    # только явным указанием каталога, на время разбора проблемы.
    speech_debug_dir = os.environ.get("SPEECH_DEBUG_DIR", "").strip()

    speech_debug_max_seconds_raw = os.environ.get("SPEECH_DEBUG_MAX_SECONDS", "").strip()
    if not speech_debug_max_seconds_raw:
        speech_debug_max_seconds = _DEFAULT_SPEECH_DEBUG_MAX_SECONDS
    else:
        try:
            speech_debug_max_seconds = float(speech_debug_max_seconds_raw)
        except ValueError as exc:
            raise ConfigError(
                "Переменная окружения SPEECH_DEBUG_MAX_SECONDS должна быть числом."
            ) from exc
    if speech_debug_max_seconds <= 0:
        raise ConfigError(
            "Переменная окружения SPEECH_DEBUG_MAX_SECONDS должна быть положительным числом."
        )

    # Синтез речи (команда «повтори»). Включён по умолчанию, но модель
    # скачивается при первом обращении, а не при старте (см. bot.tts), так
    # что включённая настройка сама по себе ничего не грузит и не замедляет
    # запуск бота.
    tts_enabled = _parse_bool_env("TTS_ENABLED", default=True)
    tts_model_name = os.environ.get("TTS_MODEL_NAME", "").strip() or _DEFAULT_TTS_MODEL_NAME

    tts_speaker_raw = os.environ.get("TTS_SPEAKER_ID", "").strip()
    if not tts_speaker_raw:
        tts_speaker_id = _DEFAULT_TTS_SPEAKER_ID
    else:
        try:
            tts_speaker_id = int(tts_speaker_raw)
        except ValueError as exc:
            raise ConfigError(
                "Переменная окружения TTS_SPEAKER_ID должна быть целым числом."
            ) from exc
    if tts_speaker_id < 0:
        raise ConfigError("Переменная окружения TTS_SPEAKER_ID не может быть отрицательной.")

    # Голосовые ответы бота на голосовые команды («Пауза», «Далее» и т. п.,
    # см. bot.voice_replies) — украшение поверх самих команд, а не их часть.
    # Включены по умолчанию вместе с TTS_ENABLED; выключаются отдельно, если
    # нужна только сама голосовая команда без болтовни бота в ответ.
    voice_replies = _parse_bool_env("VOICE_REPLIES", default=True)

    return Config(
        discord_token=discord_token,
        guild_id=guild_id,
        yandex_token=yandex_token,
        log_level=log_level,
        default_volume=default_volume,
        ffmpeg_path=ffmpeg_path,
        idle_timeout=idle_timeout,
        nekto_token=nekto_token,
        nekto_user_agent=nekto_user_agent,
        speech_model_path=speech_model_path,
        speech_enabled=speech_enabled,
        speech_transcript=speech_transcript,
        speech_debug_dir=speech_debug_dir,
        speech_debug_max_seconds=speech_debug_max_seconds,
        tts_enabled=tts_enabled,
        tts_model_name=tts_model_name,
        tts_speaker_id=tts_speaker_id,
        voice_replies=voice_replies,
    )
