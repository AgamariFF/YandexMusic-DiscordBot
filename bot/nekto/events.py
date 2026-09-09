"""События сессии nekto.me, о которых вызывающий код узнаёт через `NektoSession.events()`."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PeerFoundEvent:
    """Собеседник найден, соединение с ним нужно поддерживать по `connection_id`."""

    connection_id: str
    # Кто из сторон первым отправляет offer — см. `bot.nekto.webrtc.WebRtcLink.start`.
    initiator: bool


@dataclass(frozen=True, slots=True)
class PeerLeftEvent:
    """Собеседник покинул разговор — по своей инициативе либо из-за обрыва WebRTC-соединения."""


@dataclass(frozen=True, slots=True)
class BannedEvent:
    """Аккаунт, работающий под текущим токеном, заблокирован сервисом."""

    ban_info: Any


@dataclass(frozen=True, slots=True)
class ProtocolErrorEvent:
    """Сервис прислал сообщение типа "error", либо внутри модуля возникла протокольная ошибка."""

    error_id: Any
    description: str


NektoEvent = PeerFoundEvent | PeerLeftEvent | BannedEvent | ProtocolErrorEvent
