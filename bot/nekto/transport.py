"""Транспорт nekto.me: Socket.IO-соединение, рукопожатие (register → registered → web-agent) и
шифрование прикладных сообщений (e-socket, см. `bot.nekto.esocket` и docs/nekto-esocket.md).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import socketio

from bot.nekto.errors import NektoBannedError, NektoConnectError, NektoProtocolError
from bot.nekto.esocket import (
    MODE_REGISTER,
    MODE_SESSION,
    REGISTER_SECRET,
    decrypt_packet,
    encrypt_packet,
    is_encrypted_packet,
)
from bot.nekto.protocol import (
    ENDPOINT,
    SOCKETIO_EVENT,
    SOCKETIO_PATH,
    TRANSPORTS,
    TYPE_BAN,
    TYPE_REGISTERED,
    build_register_message,
    build_web_agent_message,
    compute_web_agent_signature,
)

logger = logging.getLogger(__name__)

DEFAULT_HANDSHAKE_TIMEOUT = 15.0

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


def _split_socketio_packets(data: str) -> list[str]:
    """Разделяет склеенные Socket.IO-пакеты, которые возвращает nekto.me."""
    packets: list[str] = []
    offset = 0
    decoder = json.JSONDecoder()
    while offset < len(data):
        while offset < len(data) and data[offset] in ",\" \r\n\t":
            offset += 1
        if offset == len(data):
            break
        if not data[offset].isdigit():
            logger.warning("Некорректный хвост Socket.IO-буфера nekto.me: %r", data[offset:])
            break
        json_start = next(
            (index for index in range(offset, len(data)) if data[index] in "[{"),
            None,
        )
        if json_start is None:
            packets.append(data[offset:])
            break
        try:
            _, json_end = decoder.raw_decode(data, json_start)
        except json.JSONDecodeError:
            return [data]
        packets.append(data[offset:json_end])
        offset = json_end
    return packets or [data]


class _NektoSocketIOClient(socketio.AsyncClient):
    """Socket.IO-клиент с поддержкой склеенных кадров nekto.me."""

    async def _handle_eio_message(self, data: Any) -> None:
        if isinstance(data, str):
            packets = _split_socketio_packets(data)
            if not packets:
                return
            if len(packets) > 1:
                for packet_data in packets:
                    await super()._handle_eio_message(packet_data)
                return
        try:
            await super()._handle_eio_message(data)
        except json.JSONDecodeError:
            logger.warning("Некорректный Socket.IO-пакет nekto.me: %r", data)


class NektoTransport:
    """Устанавливает Socket.IO-соединение с nekto.me и проводит рукопожатие register/web-agent.

    После успешного `connect()` все входящие сообщения протокола, КРОМЕ
    служебного "registered" (оно целиком обрабатывается здесь же, во время
    рукопожатия — см. `_handle_registered`), передаются во внешний
    обработчик, установленный через `set_message_handler`. Исключения
    `socketio` наружу не пропускаются — вызывающий код получает только
    доменные ошибки модуля.

    Все прикладные сообщения на проводе завёрнуты в шифрованный пакет
    e-socket (см. `bot.nekto.esocket` и docs/nekto-esocket.md) — этот класс
    отвечает и за выбор ключа: "register" всегда шифруется зашитым
    секретом, а всё остальное — сессионным ключом, который сервис присылает
    в ответе "registered". Пока сессионный ключ не получен, исходящие
    сообщения из `send()` копятся в `_pending_messages` и уходят разом,
    как только ключ появляется.
    """

    def __init__(
        self,
        token: str,
        *,
        user_agent: str,
        timezone: str,
        locale: str,
    ) -> None:
        """Запоминает токен и параметры рукопожатия; само соединение открывается в `connect()`."""
        self._token = token
        self._user_agent = user_agent
        self._timezone = timezone
        self._locale = locale
        self._sio = _NektoSocketIOClient()
        self._handler: MessageHandler | None = None
        self._registered = asyncio.Event()
        self._handshake_error: Exception | None = None
        self._closed = False
        # Сессионный ключ шифрования e-socket приходит от сервера в ответе
        # "registered" (см. `_handle_registered`); до этого момента он None,
        # и всё, что просят отправить через `send()`, копится в очереди.
        self._session_key: str | None = None
        self._pending_messages: list[dict[str, Any]] = []
        self._register_socketio_handlers()

    def set_message_handler(self, handler: MessageHandler) -> None:
        """Регистрирует обработчик сообщений протокола, приходящих после рукопожатия."""
        self._handler = handler

    async def connect(self) -> None:
        """Открывает WebSocket-соединение и дожидается завершения рукопожатия целиком.

        Само рукопожатие асинхронное и управляется колбэками Socket.IO
        (`_on_connect`, `_on_message`), поэтому здесь мы лишь ждём, пока
        внутренний `_registered` не будет установлен либо колбэк не
        зафиксирует ошибку в `_handshake_error` — а не выполняем шаги
        рукопожатия последовательно сами. `DEFAULT_HANDSHAKE_TIMEOUT`
        защищает от зависания, если сервис принял соединение, но дальше
        молчит (не прислал "registered" и не разорвал соединение сам).
        Таймаут не вынесен в параметр функции: если вызывающему коду нужен
        другой лимит, он оборачивает сам вызов в свой `asyncio.timeout(...)`
        — это композируется с внутренним таймаутом естественным образом
        (сработает тот, что короче).
        """
        self._registered.clear()
        self._handshake_error = None
        # Сброс на каждое подключение: сессионный ключ прошлого соединения
        # (если было) больше не действителен, а сообщения, не успевшие уйти
        # до разрыва, нельзя тащить в новую сессию — сервер их не поймёт.
        self._session_key = None
        self._pending_messages = []
        try:
            async with asyncio.timeout(DEFAULT_HANDSHAKE_TIMEOUT):
                await self._sio.connect(
                    ENDPOINT,
                    transports=TRANSPORTS,
                    socketio_path=SOCKETIO_PATH,
                    headers={
                        "Origin": "https://nekto.me",
                        "User-Agent": self._user_agent,
                    },
                )
        except TimeoutError as exc:
            raise NektoConnectError(
                "Таймаут установки Socket.IO-соединения с nekto.me",
                user_message="Не удалось подключиться к чат-рулетке: сервис не отвечает.",
            ) from exc
        except socketio.exceptions.SocketIOError as exc:
            raise NektoConnectError(
                f"Не удалось установить Socket.IO-соединение с nekto.me: "
                f"{type(exc).__name__}: {exc}",
                user_message="Не удалось подключиться к чат-рулетке.",
            ) from exc

        try:
            async with asyncio.timeout(DEFAULT_HANDSHAKE_TIMEOUT):
                await self._registered.wait()
        except TimeoutError as exc:
            await self._safe_disconnect()
            raise NektoConnectError(
                "Таймаут ожидания подтверждения регистрации от nekto.me",
                user_message="Не удалось подключиться к чат-рулетке: сервис не отвечает.",
            ) from exc

        if self._handshake_error is not None:
            error = self._handshake_error
            self._handshake_error = None
            await self._safe_disconnect()
            raise error

        logger.info("Рукопожатие с nekto.me завершено")

    async def send(self, payload: dict[str, Any]) -> None:
        """Отправляет прикладное сообщение протокола, зашифровав его сессионным ключом.

        Пока сессионный ключ не получен от сервера (ответ "registered" ещё в
        пути), шифровать сообщение нечем, а отправлять его открытым текстом
        сервис не примет — поэтому оно копится в `_pending_messages` и
        уходит целиком, как только `_handle_registered` заполнит ключ.
        """
        if self._session_key is None:
            self._pending_messages.append(payload)
            return
        await self._emit_encrypted(payload, secret=self._session_key, mode=MODE_SESSION)

    async def close(self) -> None:
        """Идемпотентно закрывает Socket.IO-соединение."""
        if self._closed:
            return
        self._closed = True
        await self._safe_disconnect()

    async def _safe_disconnect(self) -> None:
        """Отключает Socket.IO-клиент, проглатывая ошибки — используется в путях закрытия."""
        try:
            await self._sio.disconnect()
        except Exception:
            logger.warning("Ошибка при отключении Socket.IO от nekto.me", exc_info=True)

    def _register_socketio_handlers(self) -> None:
        """Регистрирует колбэки Socket.IO: старт рукопожатия и разбор входящих сообщений."""

        @self._sio.event
        async def connect() -> None:
            logger.debug(
                "Socket.IO-соединение с nekto.me открыто, отправляем зашифрованный register"
            )
            await self._emit_encrypted(
                build_register_message(
                    self._token,
                    timezone=self._timezone,
                    locale=self._locale,
                    user_agent=self._user_agent,
                ),
                secret=REGISTER_SECRET,
                mode=MODE_REGISTER,
            )

        @self._sio.on(SOCKETIO_EVENT)
        async def on_event(payload: dict[str, Any]) -> None:
            await self._on_message(payload)

    async def _emit_packet(self, packet: dict[str, Any]) -> None:
        """Отправляет уже собранный пакет (зашифрованный или служебный) событием "event"."""
        try:
            await self._sio.emit(SOCKETIO_EVENT, data=packet)
        except socketio.exceptions.SocketIOError as exc:
            raise NektoConnectError(
                f"Не удалось отправить сообщение nekto.me: {type(exc).__name__}: {exc}",
                user_message="Связь с чат-рулеткой потеряна.",
            ) from exc

    async def _emit_encrypted(self, message: dict[str, Any], *, secret: str, mode: int) -> None:
        """Шифрует сообщение под нужный режим/ключ (см. `bot.nekto.esocket`) и отправляет его."""
        await self._emit_packet(encrypt_packet(message, secret=secret, mode=mode))

    async def _flush_pending_messages(self, *, session_key: str) -> None:
        """Отправляет сообщения, накопленные в `send()`, пока сессионного ключа ещё не было."""
        pending, self._pending_messages = self._pending_messages, []
        for message in pending:
            await self._emit_encrypted(message, secret=session_key, mode=MODE_SESSION)

    def _decrypt_incoming(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Выбирает ключ по режиму пакета (`_`) и расшифровывает его через `bot.nekto.esocket`."""
        mode = packet.get("_")
        if mode == MODE_REGISTER:
            secret = REGISTER_SECRET
        elif mode == MODE_SESSION:
            if self._session_key is None:
                raise NektoProtocolError(
                    "Получен зашифрованный пакет nekto.me до появления сессионного ключа",
                    user_message="Сервис чат-рулетки вернул неожиданные данные.",
                )
            secret = self._session_key
        else:
            raise NektoProtocolError(
                f"Неизвестный режим шифрования пакета nekto.me: {mode!r}",
                user_message="Сервис чат-рулетки вернул неожиданные данные.",
            )
        return decrypt_packet(packet, secret=secret)

    async def _on_message(self, payload: dict[str, Any]) -> None:
        """Разбирает входящее сообщение: "registered" завершает рукопожатие, остальное — наружу.

        Входящий пакет сперва проверяется на признаки шифрования (см.
        `is_encrypted_packet`) — не все входящие сообщения зашифрованы,
        например ответ "registered" несёт сессионный ключ открытым текстом.
        Если пакет зашифрован, но расшифровать не удалось (битые данные,
        неверный ключ), пакет молча пропускается: соединение это не рвёт и
        исключений библиотеки шифрования наружу не выпускает.
        """
        if is_encrypted_packet(payload):
            try:
                payload = self._decrypt_incoming(payload)
            except NektoProtocolError:
                logger.warning(
                    "Не удалось расшифровать пакет nekto.me, пакет пропущен", exc_info=True
                )
                return

        message_type = payload.get("type")

        if message_type == TYPE_REGISTERED:
            await self._handle_registered(payload)
            return

        if message_type == TYPE_BAN and not self._registered.is_set():
            # Проверить со стороны клиента, бывает ли на практике бан раньше
            # "registered", нельзя — но обработать такой порядок дешевле,
            # чем считать его невозможным: без этого connect() просто
            # завис бы до таймаута вместо понятной ошибки.
            self._handshake_error = NektoBannedError(
                user_message="Аккаунт заблокирован сервисом чат-рулетки."
            )
            self._registered.set()
            return

        if self._handler is not None:
            await self._handler(payload)

    async def _handle_registered(self, payload: dict[str, Any]) -> None:
        """Обрабатывает "registered": принимает сессионный ключ и отправляет "web-agent".

        Поле "s" ответа — не шифротекст, а сам сессионный ключ e-socket
        строкой (см. docs/nekto-esocket.md); дальше им шифруется всё, кроме
        уже отправленного "register". Ключ не логируется.
        """
        internal_id = payload.get("internal_id")
        if internal_id is None:
            self._handshake_error = NektoProtocolError(
                "Ответ registered не содержит internal_id",
                user_message="Сервис чат-рулетки вернул неожиданный ответ.",
            )
            self._registered.set()
            return

        session_key = payload.get("s")
        if not isinstance(session_key, str) or not session_key:
            self._handshake_error = NektoProtocolError(
                "Ответ registered не содержит сессионный ключ шифрования",
                user_message="Сервис чат-рулетки вернул неожиданный ответ.",
            )
            self._registered.set()
            return
        self._session_key = session_key

        signature = compute_web_agent_signature(self._token, internal_id)
        try:
            await self._emit_encrypted(
                build_web_agent_message(signature), secret=session_key, mode=MODE_SESSION
            )
        except NektoConnectError as exc:
            self._handshake_error = NektoConnectError(
                f"Не удалось отправить web-agent nekto.me: {exc}",
                user_message="Не удалось подключиться к чат-рулетке.",
            )
            self._registered.set()
            return

        await self._flush_pending_messages(session_key=session_key)
        self._registered.set()
