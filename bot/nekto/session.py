"""Публичная точка входа модуля: `NektoSession` — сессия голосовой чат-рулетки nekto.me."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from enum import StrEnum
from typing import Any

import av

from bot.nekto.audio import IncomingAudioSink, OutgoingAudioTrack
from bot.nekto.errors import NektoBannedError, NektoStateError
from bot.nekto.events import (
    BannedEvent,
    CaptchaRequiredEvent,
    NektoEvent,
    PeerFoundEvent,
    PeerLeftEvent,
    ProtocolErrorEvent,
)
from bot.nekto.protocol import (
    DEFAULT_LOCALE,
    DEFAULT_TIMEZONE,
    TYPE_ANSWER,
    TYPE_BAN,
    TYPE_CAPTCHA_REQUEST,
    TYPE_ERROR,
    TYPE_ICE_CANDIDATE,
    TYPE_OFFER,
    TYPE_PEER_CONNECT,
    TYPE_PEER_DISCONNECT,
    TYPE_USERS_COUNT,
    SearchCriteria,
    build_peer_disconnect_message,
    build_scan_message,
    build_stop_scan_message,
)
from bot.nekto.transport import NektoTransport
from bot.nekto.webrtc import WebRtcLink

logger = logging.getLogger(__name__)


class _State(StrEnum):
    """Внутреннее состояние сессии, определяющее, какие операции сейчас допустимы."""

    NEW = "new"
    CONNECTED = "connected"
    SEARCHING = "searching"
    IN_CALL = "in_call"
    CLOSED = "closed"


class NektoSession:
    """Сессия голосовой чат-рулетки nekto.me: транспорт, WebRTC и обмен аудио одним объектом.

    Не зависит от discord.py — Discord-слой поверх этого класса должен
    сам читать голосовой канал и передавать фреймы в `send_audio`, а также
    забирать их из `receive_audio` и класть в голосовой канал. Все
    состояния (собеседник найден/ушёл, бан, ошибка протокола) приходят
    через `events()`, не нужно лезть во внутренности транспорта или
    WebRTC.
    """

    def __init__(
        self,
        token: str,
        *,
        user_agent: str,
        criteria: SearchCriteria | None = None,
        timezone: str = DEFAULT_TIMEZONE,
        locale: str = DEFAULT_LOCALE,
    ) -> None:
        """Запоминает токен, user-agent и критерии поиска. Соединение открывает `connect()`.

        `user_agent` должен быть тем же, с которым владелец получал токен в
        браузере — сервис его проверяет (см. модульный докстринг
        `bot.nekto.transport`). Токен нигде здесь не логируется.
        """
        self._token = token
        self._user_agent = user_agent
        self._criteria = criteria if criteria is not None else SearchCriteria()
        self._timezone = timezone
        self._locale = locale

        self._lock = asyncio.Lock()
        self._state = _State.NEW
        self._transport: NektoTransport | None = None
        self._link: WebRtcLink | None = None
        self._connection_id: str | None = None
        self._banned = False

        self._events: asyncio.Queue[NektoEvent | None] = asyncio.Queue()
        self._outgoing_track = OutgoingAudioTrack()
        self._incoming_audio = IncomingAudioSink()

    @property
    def state(self) -> str:
        """Текущее состояние сессии ("new"/"connected"/"searching"/"in_call"/"closed")."""
        return self._state.value

    @property
    def connection_id(self) -> str | None:
        """Идентификатор соединения с текущим собеседником, либо None, если разговора нет."""
        return self._connection_id

    async def connect(self) -> None:
        """Подключается к nekto.me: открывает Socket.IO-соединение и проводит рукопожатие целиком.

        Внутренний таймаут защищает от зависания, если сервис принял
        соединение, но дальше молчит — см.
        `bot.nekto.transport.NektoTransport.connect`.
        """
        async with self._lock:
            if self._state is not _State.NEW:
                raise NektoStateError(
                    f"connect() вызван повторно в состоянии {self._state}",
                    user_message="Чат-рулетка уже подключена.",
                )
            transport = NektoTransport(
                self._token,
                user_agent=self._user_agent,
                timezone=self._timezone,
                locale=self._locale,
            )
            transport.set_message_handler(self._handle_message)
            # Транспорт запоминаем ДО ожидания рукопожатия, а не после: как
            # только сокет фактически подключится, входящие сообщения могут
            # начать поступать в `_handle_message` ещё до того, как эта
            # корутина вернёт управление, а обработчику нужен доступ к
            # транспорту, чтобы отвечать. Состояние `NEW` при этом ещё не
            # изменено — `_handle_message` не обрабатывает сообщения, пока
            # `_state` не сменится на `CONNECTED` (см. её начало).
            self._transport = transport
            try:
                await transport.connect()
            except Exception:
                self._transport = None
                raise
            self._state = _State.CONNECTED

    async def start_search(self, criteria: SearchCriteria | None = None) -> None:
        """Начинает поиск собеседника.

        Нельзя вызвать во время уже идущего разговора — это ошибка
        использования API (`NektoStateError`), а не что-то, что стоит
        молча превращать в мусор протокола (пересылка "scan-for-peer" при
        живом собеседнике серверу ничего разумного не говорит).
        Единственный способ сменить собеседника во время разговора —
        `next_peer()`.
        """
        async with self._lock:
            if self._state is _State.IN_CALL:
                raise NektoStateError(
                    "start_search вызван во время активного разговора",
                    user_message="Сейчас идёт разговор с собеседником. Сначала завершите его.",
                )
            self._require_active()
            await self._send_search_locked(criteria if criteria is not None else self._criteria)

    async def next_peer(self) -> None:
        """Завершает разговор с текущим собеседником (если он есть) и сразу ищет нового."""
        async with self._lock:
            self._require_active()
            await self._cancel_current_activity_locked()
            await self._send_search_locked(self._criteria)

    async def receive_audio(self) -> av.AudioFrame:
        """Возвращает очередной аудиофрейм собеседника, ожидая его при необходимости.

        Формат фрейма — тот, что реально пришёл через WebRTC (см.
        `bot.nekto.audio.IncomingAudioSink`), приведение к формату Discord —
        ответственность вызывающего кода.
        """
        return await self._incoming_audio.get_frame()

    def send_audio(self, frame: av.AudioFrame) -> None:
        """Отправляет аудиофрейм собеседнику.

        Не блокирующий: ожидается PCM 48 кГц/16 бит/стерео, кадрами по
        20 мс (см. `bot.nekto.audio`). Если собеседника сейчас нет, фрейм
        просто копится во внутренней очереди и будет отброшен при
        переполнении — вызывающему коду не нужно самому проверять,
        установлен ли сейчас разговор.
        """
        self._outgoing_track.push_frame(frame)

    async def events(self) -> AsyncIterator[NektoEvent]:
        """Асинхронно перебирает события сессии: собеседник найден/ушёл, бан, ошибка протокола.

        Останавливается сам, когда сессия закрыта через `close()` и очередь
        накопленных до этого событий исчерпана — повторно вызывать не
        нужно и небезопасно (внутренняя очередь одноразовая).
        """
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        """Идемпотентно закрывает сессию: WebRTC-соединение и Socket.IO-сокет."""
        async with self._lock:
            if self._state is _State.CLOSED:
                return
            await self._close_link()
            if self._transport is not None:
                await self._transport.close()
            self._connection_id = None
            self._state = _State.CLOSED
        await self._events.put(None)

    def _require_active(self) -> None:
        """Проверяет, что сессия подключена и ещё не закрыта."""
        if self._transport is None or self._state in (_State.NEW, _State.CLOSED):
            raise NektoStateError(
                "Операция вызвана до connect() или после close()",
                user_message="Чат-рулетка сейчас не подключена.",
            )

    async def _send_search_locked(self, criteria: SearchCriteria) -> None:
        """Отправляет "scan-for-peer" и переводит сессию в состояние поиска."""
        if self._banned:
            raise NektoBannedError()
        await self._require_transport().send(build_scan_message(criteria))
        self._criteria = criteria
        self._state = _State.SEARCHING

    async def _cancel_current_activity_locked(self) -> None:
        """Прерывает текущую активность (разговор или поиск) правильным сообщением протокола.

        Если собеседник уже найден — уходит "peer-disconnect" с его
        connectionId; если ещё идёт поиск — "stop-scan" (у поиска ещё нет
        connectionId, слать ему "peer-disconnect" бессмысленно). Если
        сессия просто подключена и ничего не происходит — отправлять
        нечего.
        """
        transport = self._require_transport()
        if self._state is _State.IN_CALL and self._connection_id is not None:
            await transport.send(build_peer_disconnect_message(self._connection_id))
            await self._close_link()
        elif self._state is _State.SEARCHING:
            await transport.send(build_stop_scan_message())
        self._connection_id = None
        self._state = _State.CONNECTED

    async def _close_link(self) -> None:
        """Закрывает текущий WebRTC-канал, если он есть."""
        link = self._link
        self._link = None
        if link is not None:
            await link.close()

    async def _on_link_closed(self, connection_id: str) -> None:
        """Колбэк `WebRtcLink` при переходе pc в failed/closed не по нашей инициативе."""
        async with self._lock:
            if self._connection_id != connection_id:
                return
            self._link = None
            self._connection_id = None
            if self._state is _State.IN_CALL:
                self._state = _State.CONNECTED
        await self._push_event(PeerLeftEvent())

    async def _handle_message(self, payload: dict[str, Any]) -> None:
        """Разбирает сообщение протокола, пришедшее после рукопожатия, и обновляет состояние."""
        message_type = payload.get("type")
        async with self._lock:
            if self._state in (_State.NEW, _State.CLOSED):
                # Сообщение пришло до того, как `connect()` пометил сессию
                # подключённой (короткое окно гонки, см. комментарий в
                # `connect()`), либо уже после `close()` — до полного
                # закрытия сокета в проводе могло остаться недоставленное
                # сообщение. Обрабатывать его в обоих случаях бессмысленно.
                logger.debug(
                    "Сообщение %s получено вне активного состояния сессии (%s), игнорируем",
                    message_type,
                    self._state,
                )
                return
            if message_type == TYPE_PEER_CONNECT:
                await self._handle_peer_connect(payload)
            elif message_type == TYPE_PEER_DISCONNECT:
                await self._handle_peer_disconnect(payload)
            elif message_type == TYPE_BAN:
                await self._handle_ban(payload)
            elif message_type == TYPE_ERROR:
                await self._handle_error(payload)
            elif message_type in (TYPE_OFFER, TYPE_ANSWER, TYPE_ICE_CANDIDATE):
                await self._handle_signaling(message_type, payload)
            elif message_type == TYPE_CAPTCHA_REQUEST:
                await self._handle_captcha_request(payload)
            elif message_type == TYPE_USERS_COUNT:
                # Тип известен намеренно, обрабатывать нечего — просто
                # статистика сервиса (см. `TYPE_USERS_COUNT`), не мешаем
                # логу пометкой "неизвестный тип".
                logger.debug("Статистика nekto.me (users-count): %r", payload)
            else:
                logger.debug("Неизвестный тип сообщения nekto.me: %r", message_type)

    async def _handle_peer_connect(self, payload: dict[str, Any]) -> None:
        """Обрабатывает "peer-connect": заводит WebRTC-канал с найденным собеседником."""
        connection_id = payload.get("connectionId")
        if not connection_id:
            logger.warning("peer-connect без connectionId, игнорируем: %r", payload)
            return
        initiator = bool(payload.get("initiator"))

        await self._close_link()
        self._connection_id = connection_id
        self._state = _State.IN_CALL
        link = WebRtcLink(
            connection_id,
            send=self._require_transport().send,
            outgoing_track=self._outgoing_track,
            incoming_audio=self._incoming_audio,
            on_closed=self._on_link_closed,
        )
        self._link = link
        await self._push_event(PeerFoundEvent(connection_id=connection_id, initiator=initiator))

        try:
            await link.start(initiator=initiator)
        except Exception:
            logger.exception(
                "Ошибка установления WebRTC-соединения с собеседником %s", connection_id
            )
            await self._push_event(
                ProtocolErrorEvent(
                    error_id=None, description="Не удалось установить голосовое соединение"
                )
            )
            await self._cancel_current_activity_locked()

    async def _handle_peer_disconnect(self, payload: dict[str, Any]) -> None:
        """Обрабатывает "peer-disconnect": собеседник ушёл, освобождает WebRTC-канал."""
        connection_id = payload.get("connectionId")
        if self._connection_id is not None and connection_id not in (None, self._connection_id):
            logger.debug(
                "peer-disconnect для устаревшего connectionId, игнорируем: %s", connection_id
            )
            return
        await self._close_link()
        self._connection_id = None
        if self._state is _State.IN_CALL:
            self._state = _State.CONNECTED
        await self._push_event(PeerLeftEvent())

    async def _handle_ban(self, payload: dict[str, Any]) -> None:
        """Обрабатывает "ban": запоминает блокировку и уведомляет вызывающий код."""
        self._banned = True
        await self._push_event(BannedEvent(ban_info=payload.get("banInfo")))

    async def _handle_error(self, payload: dict[str, Any]) -> None:
        """Обрабатывает "error": сервис прислал протокольную ошибку, не обязательно фатальную."""
        await self._push_event(
            ProtocolErrorEvent(
                error_id=payload.get("id"), description=str(payload.get("description", ""))
            )
        )

    async def _handle_captcha_request(self, payload: dict[str, Any]) -> None:
        """Обрабатывает "captcha-request": сервис не ставит в очередь, пока капча не пройдена."""
        await self._push_event(CaptchaRequiredEvent(captcha_type=payload.get("captchaType")))

    async def _handle_signaling(self, message_type: str, payload: dict[str, Any]) -> None:
        """Делегирует сигнальные сообщения WebRTC ("offer"/"answer"/"ice-candidate") каналу."""
        if self._link is None:
            logger.debug(
                "Сигнальное сообщение %s без активного WebRTC-соединения, игнорируем",
                message_type,
            )
            return
        try:
            if message_type == TYPE_OFFER:
                await self._link.handle_offer(payload)
            elif message_type == TYPE_ANSWER:
                await self._link.handle_answer(payload)
            elif message_type == TYPE_ICE_CANDIDATE:
                await self._link.handle_ice_candidate(payload)
        except Exception:
            logger.exception("Ошибка обработки сигнального сообщения %s", message_type)
            await self._push_event(
                ProtocolErrorEvent(
                    error_id=None, description="Ошибка установления голосового соединения"
                )
            )

    def _require_transport(self) -> NektoTransport:
        """Возвращает активный транспорт либо бросает NektoStateError, если сессия не подключена."""
        if self._transport is None:
            raise NektoStateError(
                "Транспорт ещё не создан (connect() не вызывался или уже закрыт)",
                user_message="Чат-рулетка сейчас не подключена.",
            )
        return self._transport

    async def _push_event(self, event: NektoEvent) -> None:
        """Кладёт событие в очередь, которую перебирает `events()`."""
        await self._events.put(event)
