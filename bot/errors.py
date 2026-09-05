"""Иерархия доменных исключений бота."""

from __future__ import annotations


class BotError(Exception):
    """Базовая ошибка бота. user_message — безопасный текст для отправки в Discord."""

    default_message = "Произошла ошибка."

    def __init__(self, message: str | None = None, *, user_message: str | None = None) -> None:
        """Сохраняет внутреннее сообщение (для логов) и текст, безопасный для чата."""
        super().__init__(message if message is not None else self.default_message)
        self._user_message = user_message

    @property
    def user_message(self) -> str:
        """Безопасный для отправки в Discord текст ошибки."""
        return self._user_message if self._user_message is not None else self.default_message


class ConfigError(BotError):
    """Ошибка конфигурации бота: отсутствуют или некорректны переменные окружения."""

    default_message = "Ошибка конфигурации бота."


class NotInVoiceChannelError(BotError):
    """Пользователь, вызвавший команду, не находится в голосовом канале."""

    default_message = "Вы должны находиться в голосовом канале."


class NotConnectedError(BotError):
    """Бот не подключён к голосовому каналу на сервере."""

    default_message = "Бот не подключён к голосовому каналу."


class VoiceConnectError(BotError):
    """Не удалось установить соединение с голосовым каналом."""

    default_message = "Не удалось подключиться к голосовому каналу."


class YandexAuthError(BotError):
    """Ошибка аутентификации в сервисе Яндекс.Музыка."""

    default_message = "Ошибка авторизации в Яндекс.Музыке."


class WaveUnavailableError(BotError):
    """Станция «Моя волна» недоступна или не может быть запущена."""

    default_message = "«Моя волна» сейчас недоступна."


class TrackUnavailableError(BotError):
    """Текущий трек недоступен для воспроизведения (удалён, регион и т.п.)."""

    default_message = "Трек недоступен для воспроизведения."


class NothingPlayingError(BotError):
    """Команда требует активного воспроизведения, но сейчас ничего не играет."""

    default_message = "Сейчас ничего не воспроизводится."
