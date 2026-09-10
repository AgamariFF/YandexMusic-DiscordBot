"""Шифрование прикладных сообщений сокета nekto.me («e-socket», см. docs/nekto-esocket.md).

Сервис заворачивает каждое прикладное сообщение Socket.IO в отдельный JSON-пакет
`{"s": <base64 шифротекста>, "i": <base64 IV>, "_": <режим>}` и шифрует его AES-GCM.
Режим "2" — единственное сообщение "register", шифруется зашитым в бандле фронтенда
секретом; режим "1" — всё остальное, шифруется сессионным ключом, который сервер
присылает в ответе "registered". Модуль не делает ввода-вывода и ничего не знает про
Socket.IO или состояние сессии — только шифрует и расшифровывает словари сообщений;
выбор ключа/режима и хранение сессионного ключа — забота `bot.nekto.transport`.
"""

from __future__ import annotations

import base64
import json
import os
from hashlib import sha256
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bot.nekto.errors import NektoProtocolError

# Секрет, зашитый в бандле фронтенда аудиочата nekto.me (модуль "8b2e" файла
# app.<hash>.js, маркер поиска — строка "e-socket payload is incomplete", см.
# docs/nekto-esocket.md). Снят вручную из бандла 10 сентября 2026. Секрет живёт
# в бандле и меняется вместе с релизами фронтенда — когда это случится, вход
# снова молча зависнет на "register", как это уже было один раз; чинится
# повторным извлечением секрета по инструкции из docs/nekto-esocket.md, а не
# подбором. Значение не является нашим секретом и не относится к пользователю,
# но всё равно не должно попадать в логи или тексты исключений — по требованию
# владельца бота.
REGISTER_SECRET = "e723938b821ede7337fbe5e642693d1d9474d5bedfdcd01039693e38d424fc72"

# Режимы шифрования пакета (поле "_"): каким ключом зашифровано сообщение.
MODE_REGISTER = 2
MODE_SESSION = 1

_IV_LENGTH = 12


def _derive_key(secret: str) -> bytes:
    """Выводит 32-байтовый ключ AES-GCM из секрета: SHA-256(UTF-8(секрет))."""
    return sha256(secret.encode("utf-8")).digest()


def encrypt_packet(message: dict[str, Any], *, secret: str, mode: int) -> dict[str, Any]:
    """Шифрует прикладное сообщение в пакет e-socket `{"s": ..., "i": ..., "_": mode}`.

    `AESGCM.encrypt` из `cryptography` сама дописывает 16-байтовый тег
    аутентификации в конец шифротекста — это ровно то же поведение, что у
    WebCrypto на стороне сервиса, поэтому отдельно склеивать шифротекст с
    тегом не нужно. Это специфика конкретной библиотеки, а не стандарта:
    если её когда-нибудь заменят на реализацию, возвращающую тег отдельным
    значением (например, часть API `pycryptodome` при ручной сборке), это
    условие останется невыполненным незаметно — сообщения будут собираться
    без тега, и расшифровка на сервере станет молча ломаться. Не убирайте
    этот комментарий вместе с заменой библиотеки.
    """
    key = _derive_key(secret)
    iv = os.urandom(_IV_LENGTH)
    plaintext = json.dumps(message).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(iv, plaintext, None)
    return {
        "s": base64.b64encode(ciphertext).decode("ascii"),
        "i": base64.b64encode(iv).decode("ascii"),
        "_": mode,
    }


def is_encrypted_packet(payload: Any) -> bool:
    """Проверяет, что входящее сообщение — зашифрованный пакет e-socket.

    Клиент сервиса определяет это так же: пакетом считается только словарь,
    у которого поля "s" и "i" — строки. Служебные пакеты самого Socket.IO и
    сообщения, которые сервис по какой-то причине шлёт открытым текстом
    (например, "registered" — там "s" есть, но это сессионный ключ, а не
    шифротекст, и "i" вовсе отсутствует), этому условию не удовлетворяют и
    расшифровке не подлежат.
    """
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("s"), str)
        and isinstance(payload.get("i"), str)
    )


def decrypt_packet(payload: dict[str, Any], *, secret: str) -> dict[str, Any]:
    """Расшифровывает пакет e-socket и возвращает разобранный JSON-словарь сообщения.

    Любая неудача расшифровки (битый base64, неверный ключ, повреждённый или
    подменённый шифротекст/тег, не-JSON после расшифровки) заворачивается в
    `NektoProtocolError` — исключения `cryptography` наружу не пропускаются.
    Секрет/ключ в текст ошибки не попадают.
    """
    key = _derive_key(secret)
    try:
        iv = base64.b64decode(payload["i"], validate=True)
        ciphertext = base64.b64decode(payload["s"], validate=True)
        plaintext = AESGCM(key).decrypt(iv, ciphertext, None)
        decoded = json.loads(plaintext.decode("utf-8"))
    except (InvalidTag, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise NektoProtocolError(
            f"Не удалось расшифровать пакет e-socket nekto.me: {type(exc).__name__}",
            user_message="Сервис чат-рулетки вернул повреждённые данные.",
        ) from exc
    if not isinstance(decoded, dict):
        raise NektoProtocolError(
            "Расшифрованный пакет e-socket nekto.me — не JSON-объект",
            user_message="Сервис чат-рулетки вернул повреждённые данные.",
        )
    return decoded
