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


@dataclass(frozen=True, slots=True)
class CaptchaRequiredEvent:
    """Сервис требует пройти капчу и не ставит клиента в очередь поиска собеседника.

    Приходит вместо начала поиска в ответ на "scan-for-peer": сервис
    прислал "captcha-request" (проверено живьём, `captcha_type` в этом
    случае — "RECAPTCHA") и до прохождения капчи в очередь не ставит —
    см. докстринг `bot.nekto.protocol.SearchCriteria`. Сам модуль капчу
    не проходит и ничего с ней не делает — только сообщает наружу, чтобы
    вызывающий код (ког рулетки) мог уведомить пользователя вместо
    молчаливого бесконечного ожидания.
    """

    captcha_type: Any


NektoEvent = (
    PeerFoundEvent | PeerLeftEvent | BannedEvent | ProtocolErrorEvent | CaptchaRequiredEvent
)
