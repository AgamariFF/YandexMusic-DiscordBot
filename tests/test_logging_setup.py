"""Tests for bot.logging_setup module."""

import logging
import sys

from bot.logging_setup import SecretMaskingFilter, setup_logging


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
