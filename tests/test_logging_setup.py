"""Tests for bot.logging_setup module."""

import io
import logging
import sys

from bot.logging_setup import _MAX_MESSAGE_LENGTH, SecretMaskingFilter, setup_logging


class TestSecretMaskingFilter:
    """Tests for SecretMaskingFilter."""

    def test_secret_in_msg_is_masked(self):
        """Secret in msg is replaced with ***."""
        secret = "my-secret-token-abc123456"
        filter_obj = SecretMaskingFilter([secret])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=f"Token: {secret}",
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        assert secret not in record.getMessage()
        assert "***" in record.msg

    def test_secret_in_args_is_masked(self):
        """Secret passed via args is masked and args cleared."""
        secret = "another-secret-token-xyz789"
        filter_obj = SecretMaskingFilter([secret])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Token: %s",
            args=(secret,),
            exc_info=None,
        )
        filter_obj.filter(record)
        message = record.getMessage()
        assert secret not in message
        assert "***" in record.msg
        assert record.args is None

    def test_short_secret_not_masked(self):
        """Secrets shorter than 8 chars are not masked."""
        short_secret = "secret"
        filter_obj = SecretMaskingFilter([short_secret])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=f"Pass: {short_secret}",
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        message = record.getMessage()
        assert short_secret in message
        assert "***" not in record.msg

    def test_empty_secret_ignored(self):
        """Empty string in secrets doesn't break filter."""
        filter_obj = SecretMaskingFilter(["", "valid-secret-token-12345"])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        result = filter_obj.filter(record)
        assert result is True

    def test_multiple_different_secrets_masked(self):
        """Multiple different secrets in one message are all masked."""
        secret1 = "first-secret-token-abcdef"
        secret2 = "second-secret-token-ghijkl"
        filter_obj = SecretMaskingFilter([secret1, secret2])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=f"Secrets: {secret1} and {secret2}",
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        assert secret1 not in record.msg
        assert secret2 not in record.msg
        assert record.msg.count("***") == 2

    def test_non_string_msg_doesnt_raise(self):
        """Non-string msg doesn't raise exception."""
        filter_obj = SecretMaskingFilter(["test-secret-token"])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=12345,
            args=(),
            exc_info=None,
        )
        result = filter_obj.filter(record)
        assert result is True

    def test_secret_in_exception_text_masked(self):
        """Secret in exception traceback is masked."""
        secret = "exc-secret-token-123456"
        try:
            raise ValueError(f"Error with {secret}")
        except ValueError:
            exc_info = sys.exc_info()

        filter_obj = SecretMaskingFilter([secret])
        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Exception occurred",
            args=(),
            exc_info=exc_info,
        )
        filter_obj.filter(record)
        assert record.exc_text is not None
        assert secret not in record.exc_text
        assert "***" in record.exc_text

    def test_filter_always_returns_true(self):
        """filter() always returns True (record is not dropped)."""
        filter_obj = SecretMaskingFilter(["test-secret-token-123456"])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test",
            args=(),
            exc_info=None,
        )
        assert filter_obj.filter(record) is True

    def test_short_message_not_truncated(self):
        """Message shorter than limit passes through unchanged."""
        filter_obj = SecretMaskingFilter([])
        short_msg = "Hello world, this is a test message"
        assert len(short_msg) < _MAX_MESSAGE_LENGTH
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=short_msg,
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        assert record.msg == short_msg
        assert "обрезано" not in record.msg

    def test_long_message_truncated(self):
        """Message longer than limit is truncated with suffix."""
        filter_obj = SecretMaskingFilter([])
        long_msg = "A" * 600  # 600 > 500, so it will be truncated
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=long_msg,
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        assert "обрезано" in record.msg
        assert "600" in record.msg
        assert len(record.msg) <= _MAX_MESSAGE_LENGTH + 30  # Allow for suffix

    def test_message_exactly_at_limit_not_truncated(self):
        """Message with length exactly equal to limit is not truncated."""
        filter_obj = SecretMaskingFilter([])
        exact_msg = "X" * _MAX_MESSAGE_LENGTH
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=exact_msg,
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        assert record.msg == exact_msg
        assert "обрезано" not in record.msg

    def test_secret_masked_before_truncation(self):
        """Secret in truncated portion is masked before truncation occurs."""
        secret = "super-secret-token-123456"
        filter_obj = SecretMaskingFilter([secret])
        # Build message: part before limit, then secret that falls into truncated portion
        safe_part = "X" * (_MAX_MESSAGE_LENGTH - 10)
        secret_part = secret + "Y" * 50
        long_msg = safe_part + secret_part
        assert len(long_msg) > _MAX_MESSAGE_LENGTH
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=long_msg,
            args=(),
            exc_info=None,
        )
        filter_obj.filter(record)
        # Secret must not appear anywhere in the record
        assert secret not in record.msg
        # Check that masking occurred
        assert "***" in record.msg

    def test_traceback_not_truncated(self):
        """Traceback in exc_text is masked but not truncated."""
        secret = "traceback-secret-token-abcdef"
        filter_obj = SecretMaskingFilter([secret])
        try:
            raise ValueError(f"Error with {secret} and " + "X" * 1000)
        except ValueError:
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test",
            level=logging.ERROR,
            pathname="test.py",
            lineno=1,
            msg="Exception",
            args=(),
            exc_info=exc_info,
        )
        filter_obj.filter(record)
        # exc_text should be present
        assert record.exc_text is not None
        # Secret should be masked
        assert secret not in record.exc_text
        assert "***" in record.exc_text
        # Traceback should not be truncated (no "обрезано" suffix)
        assert "обрезано" not in record.exc_text


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_setup_logging_adds_handlers(self):
        """setup_logging adds handlers to root logger."""
        root_logger = logging.getLogger()
        initial_handlers = list(root_logger.handlers)

        try:
            setup_logging(level="INFO", secrets=(), log_file=None)
            assert len(root_logger.handlers) > 0
        finally:
            # Restore
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            for handler in initial_handlers:
                root_logger.addHandler(handler)

    def test_setup_logging_idempotent(self):
        """Repeated setup_logging calls don't duplicate handlers."""
        root_logger = logging.getLogger()
        initial_handlers = list(root_logger.handlers)

        try:
            setup_logging(level="INFO", secrets=(), log_file=None)
            handlers_after_first = list(root_logger.handlers)

            setup_logging(level="INFO", secrets=(), log_file=None)
            handlers_after_second = list(root_logger.handlers)

            assert len(handlers_after_first) == len(handlers_after_second)
        finally:
            # Restore
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            for handler in initial_handlers:
                root_logger.addHandler(handler)

    def test_setup_logging_applies_filter_to_handlers(self):
        """setup_logging applies SecretMaskingFilter to all handlers."""
        root_logger = logging.getLogger()
        initial_handlers = list(root_logger.handlers)

        try:
            secret = "setup-test-secret-token-123456"
            setup_logging(level="INFO", secrets=[secret], log_file=None)

            for handler in root_logger.handlers:
                filters = handler.filters
                assert len(filters) > 0
                assert any(isinstance(f, SecretMaskingFilter) for f in filters)
        finally:
            # Restore
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            for handler in initial_handlers:
                root_logger.addHandler(handler)

    def test_secret_in_stack_info_masked(self):
        """Secret in record.stack_info is masked."""
        secret = "stack-secret-token-123456"
        filter_obj = SecretMaskingFilter([secret])
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        record.stack_info = f"Stack trace with {secret} in it"

        filter_obj.filter(record)

        assert secret not in record.stack_info
        assert "***" in record.stack_info

    def test_setup_logging_adds_filter_to_root_logger(self):
        """setup_logging adds SecretMaskingFilter to root logger."""
        root_logger = logging.getLogger()
        initial_handlers = list(root_logger.handlers)
        initial_filters = list(root_logger.filters)

        try:
            secret = "root-logger-secret-token-123456"
            setup_logging(level="INFO", secrets=[secret], log_file=None)

            # Check that root logger has SecretMaskingFilter
            has_secret_filter = any(isinstance(f, SecretMaskingFilter) for f in root_logger.filters)
            assert has_secret_filter
        finally:
            # Restore
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            for handler in initial_handlers:
                root_logger.addHandler(handler)
            for filter_obj in root_logger.filters[:]:
                root_logger.removeFilter(filter_obj)
            for filter_obj in initial_filters:
                root_logger.addFilter(filter_obj)

    def test_console_survives_characters_outside_console_encoding(self, monkeypatch):
        """Названия треков с эмодзи и иероглифами не ломают вывод в консоль.

        На Windows консоль обычно в cp1251. Без защиты запись такого
        названия падает с UnicodeEncodeError: сообщение теряется целиком,
        а в консоль попадает трейсбек логгера вместо строки лога.
        """
        root_logger = logging.getLogger()
        initial_handlers = list(root_logger.handlers)
        initial_filters = list(root_logger.filters)
        buffer = io.BytesIO()
        console = io.TextIOWrapper(buffer, encoding="cp1251")
        monkeypatch.setattr(sys, "stdout", console)

        try:
            setup_logging(level="INFO", secrets=[], log_file=None)
            logging.getLogger("bot.test").info("Играет трек: %s", "Ϯ beatles 日本語 🎵")
            console.flush()

            assert buffer.getvalue(), "сообщение должно быть записано, а не потеряно"
            assert b"beatles" in buffer.getvalue()
        finally:
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
            for handler in initial_handlers:
                root_logger.addHandler(handler)
            for filter_obj in root_logger.filters[:]:
                root_logger.removeFilter(filter_obj)
            for filter_obj in initial_filters:
                root_logger.addFilter(filter_obj)
