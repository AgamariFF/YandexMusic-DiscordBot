"""WebRTC-часть nekto.me: одно соединение с текущим собеседником поверх aiortc."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.signaling import candidate_from_sdp
from aiortc.mediastreams import MediaStreamError

from bot.nekto.audio import IncomingAudioSink, OutgoingAudioTrack
from bot.nekto.protocol import (
    build_answer_message,
    build_offer_message,
    build_peer_connection_message,
    build_peer_mute_message,
    build_stream_received_message,
)

logger = logging.getLogger(__name__)

SendFn = Callable[[dict[str, Any]], Awaitable[None]]
OnClosedFn = Callable[[str], Awaitable[None]]

# Состояния RTCPeerConnection.connectionState, при которых соединение с
# собеседником нужно считать оконченным и закрыть аккуратно, а не ждать
# дальше.
_TERMINAL_STATES = frozenset({"failed", "closed"})


class WebRtcLink:
    """Одно WebRTC-соединение с текущим собеседником nekto.me.

    Исходящие ICE-кандидаты отдельными сообщениями сюда НЕ отправляются:
    aiortc по умолчанию (без trickle ICE) дожидается завершения сбора
    кандидатов ДО того, как корутина `setLocalDescription` вернёт
    управление, и сама включает их прямо в итоговый SDP. Поэтому наружу
    всегда уходит `pc.localDescription.sdp` — тот SDP, что осел в самом
    `RTCPeerConnection` ПОСЛЕ сбора кандидатов, а не `offer.sdp`/`answer.sdp`
    из объекта, который вернул `createOffer()`/`createAnswer()` (тот
    кандидатов ещё не содержит). Это осознанное упрощение протокола, а не
    забытая часть реализации.
    """

    def __init__(
        self,
        connection_id: str,
        *,
        send: SendFn,
        outgoing_track: OutgoingAudioTrack,
        incoming_audio: IncomingAudioSink,
        on_closed: OnClosedFn,
    ) -> None:
        """Создаёт RTCPeerConnection для одного собеседника и подписывается на его события."""
        self._connection_id = connection_id
        self._send = send
        self._outgoing_track = outgoing_track
        self._incoming_audio = incoming_audio
        self._on_closed = on_closed
        self._pc = RTCPeerConnection()
        self._closed = False
        self._track_task: asyncio.Task[None] | None = None
        self._register_handlers()

    def _register_handlers(self) -> None:
        """Подписывается на события RTCPeerConnection: входящий трек и смену состояния."""

        @self._pc.on("track")
        def on_track(track: Any) -> None:
            if track.kind != "audio":
                return
            self._track_task = asyncio.get_running_loop().create_task(
                self._handle_incoming_track(track)
            )

        @self._pc.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = self._pc.connectionState
            logger.debug("Соединение %s: connectionState=%s", self._connection_id, state)
            if state == "connected":
                await self._send(
                    build_peer_connection_message(self._connection_id, connected=True)
                )
            elif state in _TERMINAL_STATES:
                await self.close(notify=True)

    async def _handle_incoming_track(self, track: Any) -> None:
        """Уведомляет сервис о получении потока и перекладывает его фреймы в очередь наружу."""
        await self._send(build_stream_received_message(self._connection_id))
        try:
            while True:
                frame = await track.recv()
                self._incoming_audio.put_frame(frame)
        except MediaStreamError:
            # Собеседник прекратил поток (обычно вместе с закрытием pc) —
            # штатное завершение чтения трека, а не ошибка.
            pass

    async def start(self, *, initiator: bool) -> None:
        """Выполняет нашу роль сразу после "peer-connect".

        Инициатор (`initiator=True`) сразу добавляет наш трек, шлёт
        "peer-mute" и уходит в offer — все три шага целиком описаны здесь.
        Не инициатор на этом шаге не делает ничего: он добавит свой трек и
        ответит answer'ом позже, когда придёт входящий "offer" (см.
        `handle_offer`) — именно поэтому `pc.addTrack` не вызывается здесь
        для не-инициатора: добавить один и тот же трек дважды нельзя.
        """
        if not initiator:
            return
        self._pc.addTrack(self._outgoing_track)
        await self._send(build_peer_mute_message(self._connection_id, muted=False))
        offer = await self._pc.createOffer()
        await self._pc.setLocalDescription(offer)
        local = self._pc.localDescription
        await self._send(
            build_offer_message(self._connection_id, sdp=local.sdp, type_=local.type)
        )

    async def handle_offer(self, payload: dict[str, Any]) -> None:
        """Обрабатывает входящий "offer": добавляет наш трек и отвечает answer'ом."""
        remote = json.loads(payload["offer"])
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=remote["sdp"], type=remote["type"])
        )
        self._pc.addTrack(self._outgoing_track)
        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)
        local = self._pc.localDescription
        await self._send(
            build_answer_message(self._connection_id, sdp=local.sdp, type_=local.type)
        )

    async def handle_answer(self, payload: dict[str, Any]) -> None:
        """Обрабатывает входящий "answer" на наш offer."""
        remote = json.loads(payload["answer"])
        await self._pc.setRemoteDescription(
            RTCSessionDescription(sdp=remote["sdp"], type=remote["type"])
        )

    async def handle_ice_candidate(self, payload: dict[str, Any]) -> None:
        """Обрабатывает входящий "ice-candidate": разбирает строку кандидата и добавляет его."""
        raw_candidate = json.loads(payload["candidate"])["candidate"]
        candidate = candidate_from_sdp(raw_candidate["candidate"])
        candidate.sdpMid = raw_candidate.get("sdpMid")
        candidate.sdpMLineIndex = raw_candidate.get("sdpMLineIndex")
        await self._pc.addIceCandidate(candidate)

    async def close(self, *, notify: bool = False) -> None:
        """Идемпотентно закрывает RTCPeerConnection.

        `notify=True` — закрытие вызвано самим соединением (переход в
        failed/closed из `on_connectionstatechange`), и об этом нужно
        сообщить владельцу (`NektoSession`) через `on_closed`, чтобы он
        привёл состояние сессии в соответствие и не завис в ожидании
        собеседника, который уже не вернётся. `notify=False` (по умолчанию)
        — закрытие инициировано самим владельцем (`next_peer`/`close`), он
        и так об этом знает, колбэк здесь не нужен.
        """
        if self._closed:
            return
        self._closed = True
        if self._track_task is not None:
            self._track_task.cancel()
        try:
            await self._pc.close()
        except Exception:
            logger.warning(
                "Ошибка при закрытии WebRTC-соединения %s", self._connection_id, exc_info=True
            )
        if notify:
            await self._on_closed(self._connection_id)
