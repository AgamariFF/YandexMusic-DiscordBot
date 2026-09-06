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
_MAX_MESSAGE_LENGTH = 500
_TRUNCATED_FLAG = "_message_truncated"
_NOISY_LOGGER_NAMES = ("discord", "discord.gateway", "discord.voice_client", "yandex_music")


class SecretMaskingFilter(logging.Filter):
    """Маскирует секреты в отформатированном сообщении и ограничивает его длину.

    Секреты заменяются на '***', чтобы не попасть в лог в открытом виде.
    Длина сообщения ограничена отдельно: API Яндекса при ответе 429 кладёт
    в текст исключения целую HTML-страницу (капча, счётчик метрики, inline-
    скрипты) размером в несколько килобайт, и без ограничения одна такая
    запись делает лог нечитаемым.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        """Запоминает секреты для маскирования; пустые и короткие (< 8 символов) игнорируются."""
        super().__init__()
        self._secrets = [
            secret for secret in secrets if secret and len(secret) >= _MIN_SECRET_LENGTH
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Подставляет args, маскирует секреты и обрезает сообщение до предельной длины.

        Маскирование выполняется строго до обрезки: если бы порядок был обратным,
        секрет мог бы частично уцелеть в отброшенном хвосте. exc_text и
        stack_info не обрезаются — трейсбеки бывают длинными законно и нужны
        для диагностики целиком. Возвращает True.

        Одна и та же запись проходит через фильтр несколько раз (он висит на
        каждом хендлере и на root), поэтому обрезка идемпотентна — иначе
        второй проход резал бы уже обрезанный текст и записывал в пометку
        длину обрезка вместо исходной. Маскирование, наоборот, безусловно:
        пропустить его нельзя даже один раз, иначе фильтр с другим набором
        секретов, добавленный позже, выпустил бы секрет в лог.
        """
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)

        record.msg = self._truncate(record, self._mask(message))
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

    @staticmethod
    def _truncate(record: logging.LogRecord, text: str) -> str:
        """Обрезает текст до `_MAX_MESSAGE_LENGTH` символов с пометкой об обрезке.

        Признак обрезки хранится на самой записи, а не в фильтре: запись
        разделяется всеми хендлерами, и повторный проход должен вернуть тот
        же текст с той же исходной длиной в пометке.
        """
        if getattr(record, _TRUNCATED_FLAG, False):
            return text
        if len(text) <= _MAX_MESSAGE_LENGTH:
            return text
        setattr(record, _TRUNCATED_FLAG, True)
        return f"{text[:_MAX_MESSAGE_LENGTH]}… [обрезано, всего {len(text)} симв.]"


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
