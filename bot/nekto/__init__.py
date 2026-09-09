"""Публичный API модуля nekto.me: голосовая чат-рулетка, независимая от discord.py.

Discord-слой (следующий этап работы) должен использовать только то, что
экспортировано здесь, — внутренние модули (`transport`, `webrtc`, `audio`,
`protocol`) прямого использования не предполагают.
"""

from __future__ import annotations

from bot.nekto.errors import (
    NektoBannedError,
    NektoConnectError,
    NektoProtocolError,
    NektoStateError,
)
from bot.nekto.events import (
    BannedEvent,
    NektoEvent,
    PeerFoundEvent,
    PeerLeftEvent,
    ProtocolErrorEvent,
)
from bot.nekto.protocol import AgeRange, SearchCriteria
from bot.nekto.session import NektoSession

__all__ = [
    "AgeRange",
    "BannedEvent",
    "NektoBannedError",
    "NektoConnectError",
    "NektoEvent",
    "NektoProtocolError",
    "NektoSession",
    "NektoStateError",
    "PeerFoundEvent",
    "PeerLeftEvent",
    "ProtocolErrorEvent",
    "SearchCriteria",
]
