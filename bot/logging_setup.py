"""Настройка логирования: вывод в stdout/файл и маскирование секретов."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from collections.abc import Iterable
from typing import TextIO

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_MIN_SECRET_LENGTH = 8
_MASK = "***"
_NOISY_LOGGER_NAMES = ("discord", "discord.gateway", "discord.voice_client", "yandex_music")


class SecretMaskingFilter(logging.Filter):
    """Заменяет вхождения секретов в отформатированном сообщении на '***'."""

    def __init__(self, secrets: Iterable[str]) -> None:
        """Запоминает секреты для маскирования; пустые и короткие (< 8 символов) игнорируются."""
        super().__init__()
        self._secrets = [
            secret for secret in secrets if secret and len(secret) >= _MIN_SECRET_LENGTH
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Подставляет args, маскирует секреты в сообщении и очищает args. Возвращает True."""
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        record.msg = self._mask(message)
        record.args = None

        if record.exc_info:
            exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_text = self._mask(exc_text)

        if record.stack_info:
            record.stack_info = self._mask(record.stack_info)

        return True

    def _mask(self, text: str) -> str:
        """Заменяет все вхождения известных секретов в тексте на маску."""
        for secret in self._secrets:
            text = text.replace(secret, _MASK)
        return text


def _console_stream() -> TextIO:
    """Возвращает stdout, безопасный для символов вне кодировки консоли.

    На Windows консоль обычно работает в cp1251, а названия треков в
    Яндекс.Музыке содержат эмодзи и иероглифы. Без этого запись такого
    названия падает с `UnicodeEncodeError`, сообщение теряется целиком,
    а в консоль вместо него попадает трейсбек логгера.
    """
    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            logging.getLogger(__name__).debug("Не удалось настроить кодировку вывода консоли")
    return stream


def setup_logging(
    level: str = "INFO",
    secrets: Iterable[str] = (),
    log_file: str | os.PathLike[str] | None = "logs/bot.log",
) -> None:
    """Настраивает root-логгер: StreamHandler в stdout и опциональный RotatingFileHandler."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT)
    secret_filter = SecretMaskingFilter(secrets)

    handlers: list[logging.Handler] = [logging.StreamHandler(_console_stream())]

    if log_file is not None:
        log_dir = os.path.dirname(os.fspath(log_file))
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
        )

    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(secret_filter)
        root_logger.addHandler(handler)

    # Дополнительно вешаем фильтр на сам root-логгер. Это подстраховка только для
    # записей, созданных напрямую на root (logging.error(...)): записи дочерних
    # логгеров приходят через callHandlers и фильтры логгера не проходят.
    # Основную защиту даёт фильтр на каждом хендлере, он висит выше.
    for existing_filter in list(root_logger.filters):
        if isinstance(existing_filter, SecretMaskingFilter):
            root_logger.removeFilter(existing_filter)
    root_logger.addFilter(secret_filter)

    for logger_name in _NOISY_LOGGER_NAMES:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
