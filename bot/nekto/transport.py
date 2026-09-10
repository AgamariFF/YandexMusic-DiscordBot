"""Транспорт nekto.me: Socket.IO-соединение и рукопожатие (register → registered → web-agent)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import socketio

from bot.nekto.errors import NektoBannedError, NektoConnectError, NektoProtocolError
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
        """Отправляет прикладное сообщение протокола событием Socket.IO "event"."""
        try:
            await self._sio.emit(SOCKETIO_EVENT, data=payload)
        except socketio.exceptions.SocketIOError as exc:
            raise NektoConnectError(
                f"Не удалось отправить сообщение nekto.me: {type(exc).__name__}: {exc}",
                user_message="Связь с чат-рулеткой потеряна.",
            ) from exc

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
            logger.debug("Socket.IO-соединение с nekto.me открыто, отправляем register")
            await self._sio.emit(
                SOCKETIO_EVENT,
                data=build_register_message(
                    self._token,
                    timezone=self._timezone,
                    locale=self._locale,
                    user_agent=self._user_agent,
                ),
            )

        @self._sio.on(SOCKETIO_EVENT)
        async def on_event(payload: dict[str, Any]) -> None:
            await self._on_message(payload)

    async def _on_message(self, payload: dict[str, Any]) -> None:
        """Разбирает входящее сообщение: "registered" завершает рукопожатие, остальное — наружу."""
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
        """Обрабатывает "registered": считает подпись рукопожатия и отправляет "web-agent"."""
        internal_id = payload.get("internal_id")
        if internal_id is None:
            self._handshake_error = NektoProtocolError(
                "Ответ registered не содержит internal_id",
                user_message="Сервис чат-рулетки вернул неожиданный ответ.",
            )
            self._registered.set()
            return

        signature = compute_web_agent_signature(self._token, internal_id)
        try:
            await self._sio.emit(SOCKETIO_EVENT, data=build_web_agent_message(signature))
        except socketio.exceptions.SocketIOError as exc:
            self._handshake_error = NektoConnectError(
                f"Не удалось отправить web-agent nekto.me: {type(exc).__name__}: {exc}",
                user_message="Не удалось подключиться к чат-рулетке.",
            )
            self._registered.set()
            return

        self._registered.set()
