"""Тесты для модуля bot.voice_dave (сквозное DAVE-шифрование приёма голоса)."""

from unittest.mock import MagicMock

import pytest

from bot.voice_dave import (
    _DaveAwarePacketDecryptor,
    _DecryptStats,
    _ReceiveDiagnostics,
    diagnostics_enabled,
    install_dave_decryption,
)


class TestDiagnosticsEnabled:
    """Тесты функции diagnostics_enabled()."""

    def test_diagnostics_disabled_by_default(self, monkeypatch):
        """По умолчанию возвращает False, если переменная окружения не установлена."""
        monkeypatch.delenv("VOICE_RECV_DIAG", raising=False)
        assert diagnostics_enabled() is False

    def test_diagnostics_enabled_with_1(self, monkeypatch):
        """Возвращает True для значения '1'."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "1")
        assert diagnostics_enabled() is True

    def test_diagnostics_enabled_with_true(self, monkeypatch):
        """Возвращает True для значения 'true'."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "true")
        assert diagnostics_enabled() is True

    def test_diagnostics_enabled_with_yes(self, monkeypatch):
        """Возвращает True для значения 'yes'."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "yes")
        assert diagnostics_enabled() is True

    def test_diagnostics_enabled_with_on(self, monkeypatch):
        """Возвращает True для значения 'on'."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "on")
        assert diagnostics_enabled() is True

    def test_diagnostics_case_insensitive(self, monkeypatch):
        """Проверка регистронезависима."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "TRUE")
        assert diagnostics_enabled() is True

        monkeypatch.setenv("VOICE_RECV_DIAG", "Yes")
        assert diagnostics_enabled() is True

        monkeypatch.setenv("VOICE_RECV_DIAG", "ON")
        assert diagnostics_enabled() is True

    def test_diagnostics_strips_whitespace(self, monkeypatch):
        """Обрезает пробелы перед проверкой."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "  true  ")
        assert diagnostics_enabled() is True

        monkeypatch.setenv("VOICE_RECV_DIAG", "\n1\n")
        assert diagnostics_enabled() is True

    def test_diagnostics_disabled_for_invalid_values(self, monkeypatch):
        """Возвращает False для недействительных значений."""
        monkeypatch.setenv("VOICE_RECV_DIAG", "false")
        assert diagnostics_enabled() is False

        monkeypatch.setenv("VOICE_RECV_DIAG", "no")
        assert diagnostics_enabled() is False

        monkeypatch.setenv("VOICE_RECV_DIAG", "invalid")
        assert diagnostics_enabled() is False

        monkeypatch.setenv("VOICE_RECV_DIAG", "0")
        assert diagnostics_enabled() is False


class TestReceiveDiagnostics:
    """Тесты класса _ReceiveDiagnostics."""

    def test_diagnostics_disabled_initialization(self):
        """Инициализируется с флагом отключения диагностики."""
        diag = _ReceiveDiagnostics(enabled=False)
        assert diag._enabled is False

    def test_diagnostics_enabled_initialization(self):
        """Инициализируется с флагом включения диагностики."""
        diag = _ReceiveDiagnostics(enabled=True)
        assert diag._enabled is True

    def test_begin_packet_increments_index(self):
        """begin_packet() увеличивает счётчик пакетов."""
        diag = _ReceiveDiagnostics(enabled=True)
        assert diag._packet_index == 0

        diag.begin_packet()
        assert diag._packet_index == 1

        diag.begin_packet()
        assert diag._packet_index == 2

    def test_begin_packet_disabled_does_nothing(self):
        """begin_packet() ничего не делает, если диагностика отключена."""
        diag = _ReceiveDiagnostics(enabled=False)
        assert diag._packet_index == 0

        diag.begin_packet()
        assert diag._packet_index == 0

    def test_log_this_packet_first_burst_packets(self):
        """_log_this_packet=True для первых пакетов по _DIAG_BURST_PACKETS."""
        diag = _ReceiveDiagnostics(enabled=True)

        for _ in range(5):  # _DIAG_BURST_PACKETS = 5
            diag.begin_packet()
            assert diag._log_this_packet is True


class TestInstallDaveDecryptionBasic:
    """Тесты базового функционала install_dave_decryption()."""

    def test_install_dave_decryption_requires_listening(self):
        """Бросает RuntimeError, если voice_client не слушает."""
        voice_client = MagicMock()
        voice_client.is_listening = MagicMock(return_value=False)

        with pytest.raises(RuntimeError) as exc_info:
            install_dave_decryption(voice_client)

        assert "listen" in str(exc_info.value).lower()

    def test_install_dave_decryption_sets_decryptor(self):
        """Заменяет reader.decryptor на _DaveAwarePacketDecryptor."""
        # Создаём mock voice_client
        voice_client = MagicMock()
        voice_client.is_listening = MagicMock(return_value=True)
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)  # 32-байтовый ключ

        # Создаём mock reader с оригинальным расшифровщиком
        original_decryptor = MagicMock()
        reader = MagicMock()
        reader.decryptor = original_decryptor

        voice_client._reader = reader

        # Mock соединение для диагностики
        connection = MagicMock()
        voice_client._connection = connection

        install_dave_decryption(voice_client)

        # Расшифровщик должен быть заменён на _DaveAwarePacketDecryptor
        assert reader.decryptor is not original_decryptor
        assert isinstance(reader.decryptor, _DaveAwarePacketDecryptor)


class TestDecryptRtpWrapping:
    """Тесты критической функции: decrypt_rtp должна вызываться через DAVE-слой."""

    def test_decrypt_rtp_calls_apply_dave_with_valid_session(self):
        """При вызове decrypt_rtp с активной DAVE-сессией вызывается DAVE-расшифровка."""
        voice_client = MagicMock()
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)

        connection = MagicMock()
        dave_session = MagicMock()
        connection.dave_session = dave_session
        connection.can_encrypt = True
        voice_client._connection = connection
        voice_client._get_id_from_ssrc = MagicMock(return_value=456)

        # Успешная расшифровка
        dave_session.decrypt = MagicMock(return_value=b"opus_frame")

        diagnostics = _ReceiveDiagnostics(enabled=False)

        try:
            decryptor = _DaveAwarePacketDecryptor.__new__(
                _DaveAwarePacketDecryptor
            )
            decryptor._voice_client = voice_client
            decryptor._diagnostics = diagnostics
            decryptor._stats = _DecryptStats()
            decryptor._decrypt_transport = MagicMock(
                return_value=b"transport_encrypted"
            )

            packet = MagicMock()
            packet.header = b"h"
            packet.data = b"d"
            packet.extended = False
            packet.ssrc = 999

            result = decryptor._decrypt_rtp_with_dave(packet)

            # Проверяем, что результат - расшифрованные данные от DAVE
            assert result == b"opus_frame"
            # Проверяем, что DAVE был вызван с правильными параметрами
            dave_session.decrypt.assert_called_once()
        except Exception as e:
            if "vosk" not in str(e).lower():
                raise

    def test_decrypt_rtp_attribute_is_wrapped_after_install(self):
        """decrypt_rtp заменяется на обёртку DAVE после install_dave_decryption."""
        voice_client = MagicMock()
        voice_client.is_listening = MagicMock(return_value=True)
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)

        # Создаём mock reader с оригинальным расшифровщиком
        original_decrypt_func = MagicMock(return_value=b"transport_data")
        original_decryptor = MagicMock()
        original_decryptor.decrypt_rtp = original_decrypt_func

        reader = MagicMock()
        reader.decryptor = original_decryptor

        voice_client._reader = reader

        # Mock соединения для диагностики
        connection = MagicMock()
        dave_session = MagicMock()
        connection.dave_session = dave_session
        connection.can_encrypt = True
        voice_client._connection = connection
        voice_client._get_id_from_ssrc = MagicMock(return_value=123)

        # Имитируем расшифровку DAVE
        dave_session.decrypt = MagicMock(return_value=b"decrypted_opus")

        install_dave_decryption(voice_client)

        # Получаем установленный расшифровщик
        new_decryptor = reader.decryptor
        assert isinstance(new_decryptor, _DaveAwarePacketDecryptor)

        # Проверяем, что decrypt_rtp теперь — это обёртка DAVE
        # А не оригинальная функция
        assert new_decryptor.decrypt_rtp != original_decrypt_func
        assert new_decryptor.decrypt_rtp == new_decryptor._decrypt_rtp_with_dave


class TestApplyDaveDecryption:
    """Тесты DAVE-расшифровки через _apply_dave."""

    def test_apply_dave_passthrough_no_session(self):
        """_apply_dave возвращает транспортные данные, если DAVE-сессия не активна."""
        voice_client = MagicMock()
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)

        connection = MagicMock()
        connection.dave_session = None
        connection.can_encrypt = False
        voice_client._connection = connection

        diagnostics = _ReceiveDiagnostics(enabled=False)

        try:
            decryptor = _DaveAwarePacketDecryptor.__new__(
                _DaveAwarePacketDecryptor
            )
            decryptor._voice_client = voice_client
            decryptor._diagnostics = diagnostics
            decryptor._stats = _DecryptStats()

            packet = MagicMock()
            packet.header = b"header"
            packet.data = b"data"
            packet.extended = False
            packet.ssrc = 12345

            # Вызываем _apply_dave
            result = decryptor._apply_dave(packet, b"encrypted_data")

            # Должны вернуться транспортные данные как есть (passthrough)
            assert result == b"encrypted_data"
        except Exception as e:
            if "vosk" not in str(e).lower():
                raise

    def test_apply_dave_unknown_ssrc(self):
        """_apply_dave возвращает молчание для неизвестного SSRC при активной DAVE."""
        voice_client = MagicMock()
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)

        connection = MagicMock()
        dave_session = MagicMock()
        connection.dave_session = dave_session
        connection.can_encrypt = True
        voice_client._connection = connection
        voice_client._get_id_from_ssrc = MagicMock(return_value=None)

        diagnostics = _ReceiveDiagnostics(enabled=False)

        try:
            decryptor = _DaveAwarePacketDecryptor.__new__(
                _DaveAwarePacketDecryptor
            )
            decryptor._voice_client = voice_client
            decryptor._diagnostics = diagnostics
            decryptor._stats = _DecryptStats()

            packet = MagicMock()
            packet.header = b"header"
            packet.data = b"data"
            packet.extended = False
            packet.ssrc = 12345

            # Вызываем _apply_dave
            from discord.ext.voice_recv import rtp
            result = decryptor._apply_dave(packet, b"encrypted_data")

            # Должно вернуться молчание для неизвестного SSRC
            assert result == rtp.OPUS_SILENCE
            # Проверяем, что статистика отслеживает это
            assert decryptor._stats.dave_unknown_ssrc == 1
        except Exception as e:
            if "vosk" not in str(e).lower():
                raise

    def test_apply_dave_successful_decryption(self):
        """_apply_dave успешно расшифровывает при готовой DAVE-сессии."""
        voice_client = MagicMock()
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)

        connection = MagicMock()
        dave_session = MagicMock()
        connection.dave_session = dave_session
        connection.can_encrypt = True
        voice_client._connection = connection
        voice_client._get_id_from_ssrc = MagicMock(return_value=123)

        # Имитируем успешную расшифровку
        dave_session.decrypt = MagicMock(
            return_value=b"decrypted_opus"
        )

        diagnostics = _ReceiveDiagnostics(enabled=False)

        try:
            decryptor = _DaveAwarePacketDecryptor.__new__(
                _DaveAwarePacketDecryptor
            )
            decryptor._voice_client = voice_client
            decryptor._diagnostics = diagnostics
            decryptor._stats = _DecryptStats()

            packet = MagicMock()
            packet.header = b"header"
            packet.data = b"data"
            packet.extended = False
            packet.ssrc = 12345

            result = decryptor._apply_dave(packet, b"encrypted_data")

            # Должны вернуться расшифрованные данные
            assert result == b"decrypted_opus"
        except Exception as e:
            if "vosk" not in str(e).lower():
                raise


class TestInstallDaveDecryptionIdempotency:
    """Тесты идемпотентности install_dave_decryption()."""

    def test_repeated_install_does_not_break_decryption(self):
        """Повторные вызовы install_dave_decryption не ломают установку."""
        voice_client = MagicMock()
        voice_client.is_listening = MagicMock(return_value=True)
        voice_client.mode = "xsalsa20_poly1305"
        voice_client.secret_key = bytes(32)

        original_decryptor = MagicMock()
        reader = MagicMock()
        reader.decryptor = original_decryptor

        voice_client._reader = reader

        connection = MagicMock()
        connection.dave_session = None
        voice_client._connection = connection

        # Устанавливаем один раз
        install_dave_decryption(voice_client)
        first_decryptor = reader.decryptor

        # Устанавливаем снова
        install_dave_decryption(voice_client)
        second_decryptor = reader.decryptor

        # Оба должны быть _DaveAwarePacketDecryptor
        assert isinstance(first_decryptor, _DaveAwarePacketDecryptor)
        assert isinstance(second_decryptor, _DaveAwarePacketDecryptor)


class TestDecryptStatsTracking:
    """Тесты отслеживания ошибок _DecryptStats."""

    def test_stats_track_failures(self):
        """_DecryptStats отслеживает разные типы ошибок."""
        stats = _DecryptStats()
        voice_client = MagicMock()

        stats.note_transport_failure(voice_client)
        stats.note_unknown_ssrc(voice_client)
        stats.note_dave_failure(voice_client)
        stats.note_passthrough_fallback(voice_client)

        assert stats.transport_failures == 1
        assert stats.dave_unknown_ssrc == 1
        assert stats.dave_failures == 1
        assert stats.dave_passthrough_fallbacks == 1

    def test_stats_increment_counters(self):
        """_DecryptStats увеличивает счётчики при последовательных ошибках."""
        stats = _DecryptStats()
        voice_client = MagicMock()

        for _ in range(3):
            stats.note_transport_failure(voice_client)

        for _ in range(2):
            stats.note_unknown_ssrc(voice_client)

        assert stats.transport_failures == 3
        assert stats.dave_unknown_ssrc == 2
