"""Тесты для bot.nekto.esocket — шифрования прикладных сообщений e-socket nekto.me."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bot.nekto.errors import NektoProtocolError
from bot.nekto.esocket import (
    MODE_REGISTER,
    MODE_SESSION,
    decrypt_packet,
    encrypt_packet,
    is_encrypted_packet,
)

SECRET = "тестовый-секрет-e2f9c4"
MESSAGE = {"type": "scan-for-peer", "peerToPeer": True, "token": None}


def _tampered_ciphertext_packet() -> dict:
    """Готовит пакет с подменённым байтом шифротекста — общая заготовка для тестов ниже."""
    packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
    tampered = bytearray(base64.b64decode(packet["s"]))
    tampered[0] ^= 0xFF
    packet["s"] = base64.b64encode(bytes(tampered)).decode("ascii")
    return packet


class TestEncryptDecryptRoundtrip:
    """Шифрование и расшифровка одним и тем же секретом восстанавливают исходное сообщение."""

    def test_roundtrip_returns_original_message(self):
        """Расшифрованное сообщение совпадает с исходным словарём, которое было зашифровано."""
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        assert decrypt_packet(packet, secret=SECRET) == MESSAGE


class TestPacketFormat:
    """Формат пакета e-socket: ровно три поля, режим — тот, что передали."""

    def test_packet_has_exactly_three_fields(self):
        """Пакет содержит ровно поля s, i, _ и никаких других."""
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        assert set(packet.keys()) == {"s", "i", "_"}

    def test_packet_mode_matches_requested(self):
        """Значение поля _ совпадает с режимом, переданным в encrypt_packet."""
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_REGISTER)
        assert packet["_"] == MODE_REGISTER


class TestInitializationVector:
    """Вектор инициализации (IV): длина и уникальность между вызовами."""

    def test_iv_is_12_bytes_after_base64_decoding(self):
        """IV после декодирования base64 имеет длину ровно 12 байт."""
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        assert len(base64.b64decode(packet["i"])) == 12

    def test_iv_differs_between_two_consecutive_calls(self):
        """Два последовательных вызова шифрования одного сообщения дают разные IV.

        Повторное использование вектора инициализации в AES-GCM ломает всю
        защиту (позволяет восстановить открытый текст и подделать
        сообщения), поэтому IV обязан быть случайным при каждом вызове.
        """
        first = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        second = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        assert first["i"] != second["i"]


class TestKeyDerivation:
    """Ключ AES-GCM выводится именно как SHA-256 от UTF-8-представления секрета."""

    def test_ciphertext_decrypts_with_independently_derived_sha256_key(self):
        """Пакет расшифровывается ключом SHA-256(секрет), посчитанным в тесте напрямую через
        hashlib — а не результатом внутренней функции модуля.
        """
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        reference_key = hashlib.sha256(SECRET.encode("utf-8")).digest()
        iv = base64.b64decode(packet["i"])
        ciphertext = base64.b64decode(packet["s"])
        plaintext = AESGCM(reference_key).decrypt(iv, ciphertext, None)
        assert json.loads(plaintext) == MESSAGE


class TestTamperingIsRejected:
    """Повреждённые данные и неверный ключ дают доменную ошибку, а не исключение библиотеки."""

    def test_tampered_ciphertext_byte_raises_domain_error(self):
        """Подмена одного байта шифротекста ломает расшифровку доменной ошибкой (тег сработал)."""
        with pytest.raises(NektoProtocolError):
            decrypt_packet(_tampered_ciphertext_packet(), secret=SECRET)

    def test_wrong_key_raises_domain_error(self):
        """Расшифровка пакета неверным секретом тоже падает доменной ошибкой."""
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        with pytest.raises(NektoProtocolError):
            decrypt_packet(packet, secret="совсем другой секрет")


class TestIsEncryptedPacket:
    """Различение зашифрованного пакета e-socket и открытых/служебных структур."""

    def test_encrypted_packet_is_recognized(self):
        """Настоящий зашифрованный пакет (есть s и i строками) распознаётся как зашифрованный."""
        packet = encrypt_packet(MESSAGE, secret=SECRET, mode=MODE_SESSION)
        assert is_encrypted_packet(packet) is True

    def test_registered_response_is_not_recognized_as_encrypted(self):
        """Ответ registered (поле s есть, но это сессионный ключ, а не i) — не зашифрован."""
        registered = {"type": "registered", "internal_id": 1, "s": "session-key-value"}
        assert is_encrypted_packet(registered) is False

    def test_plain_service_message_is_not_recognized_as_encrypted(self):
        """Служебное сообщение без полей s/i не считается зашифрованным пакетом."""
        assert is_encrypted_packet({"type": "peer-disconnect", "connectionId": "abc"}) is False


class TestSecretDoesNotLeakIntoErrors:
    """Секрет и производный от него ключ не попадают в текст доменных ошибок."""

    def test_secret_not_in_error_internal_message(self):
        """Внутренний текст ошибки (str(exc)) не содержит значение секрета."""
        with pytest.raises(NektoProtocolError) as exc_info:
            decrypt_packet(_tampered_ciphertext_packet(), secret=SECRET)
        assert SECRET not in str(exc_info.value)

    def test_secret_not_in_error_user_message(self):
        """Пользовательский текст ошибки (user_message) не содержит значение секрета."""
        with pytest.raises(NektoProtocolError) as exc_info:
            decrypt_packet(_tampered_ciphertext_packet(), secret=SECRET)
        assert SECRET not in exc_info.value.user_message
