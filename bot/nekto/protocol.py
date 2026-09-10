"""Протокол nekto.me поверх Socket.IO: константы, формат сообщений и подпись рукопожатия.

Модуль не делает ввода-вывода — только собирает и разбирает словари
сообщений. Транспорт (Socket.IO-соединение и рукопожатие) — в
`bot.nekto.transport`, WebRTC-обмен — в `bot.nekto.webrtc`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal

# --- Транспорт ---------------------------------------------------------

ENDPOINT = "wss://audio.nekto.me/?_v=1"
SOCKETIO_PATH = "websocket"
TRANSPORTS = ["websocket"]

# Все прикладные сообщения идут одним событием Socket.IO; тип конкретного
# сообщения лежит в поле "type" полезной нагрузки, а не в имени события.
SOCKETIO_EVENT = "event"

DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_LOCALE = "ru"

# Версия протокола, которую всегда шлёт "register" — значение снято с
# рабочего клиента (см. предупреждение у `_WEB_AGENT_SALT_1` ниже), само по
# себе для нас ничего не значит.
REGISTER_VERSION = 21

# --- Типы входящих сообщений --------------------------------------------

TYPE_REGISTERED = "registered"
TYPE_PEER_CONNECT = "peer-connect"
TYPE_PEER_DISCONNECT = "peer-disconnect"
TYPE_BAN = "ban"
TYPE_ERROR = "error"
TYPE_OFFER = "offer"
TYPE_ANSWER = "answer"
TYPE_ICE_CANDIDATE = "ice-candidate"
# Приходит регулярно и независимо от поиска — несёт статистику сервиса
# ("usersCount"/"waitingUsersCount"/"talkingUsersCount"), для работы самой
# сессии не нужна. Тип известен намеренно, чтобы не засорять лог пометкой
# "неизвестный тип" (см. `NektoSession._handle_message`).
TYPE_USERS_COUNT = "users-count"
# Ответ на "scan-for-peer" вместо начала поиска: сервис требует пройти
# капчу (проверено живьём: `captchaType: "RECAPTCHA"`) и НЕ ставит клиента
# в очередь поиска собеседника, пока капча не пройдена — см.
# `bot.nekto.events.CaptchaRequiredEvent`.
TYPE_CAPTCHA_REQUEST = "captcha-request"

# --- Подпись рукопожатия (web-agent) ------------------------------------
#
# Эти две строки — не наш секрет, а часть клиентской подписи, снятой с
# рабочего протокола сервиса nekto.me (см. `compute_web_agent_signature`
# ниже): сервер сверяет подпись при получении сообщения "web-agent" и, если
# формула не совпадёт, дальше поиска собеседника не пускает. Значения
# подобраны не нами — они сняты дампом рабочего клиента, а не придуманы, и
# если сервис когда-нибудь сменит их на своей стороне, вход перестанет
# работать. Починить это можно только заново сняв актуальные значения с
# работающего клиента (браузер + devtools), а не подбором. Это самое
# хрупкое место всего модуля — не трогайте без крайней необходимости.
_WEB_AGENT_SALT_1 = "BYdKPTYYGZ7ALwA"
_WEB_AGENT_SALT_2 = "8oNm2"


def compute_web_agent_signature(user_id: str, internal_id: Any) -> str:
    """Считает подпись "web-agent" по токену и internal_id из ответа "registered".

    Формула — `base64(hex(sha256(user_id + salt1 + salt2 + internal_id)))` —
    снята с рабочего протокола, а не придумана нами (см. предупреждение у
    `_WEB_AGENT_SALT_1`). `user_id` здесь — тот же токен, что ушёл в
    "register" полем "userId", отдельного идентификатора у клиента нет.
    Функция не логирует и никуда не кладёт сам токен — это забота
    вызывающего кода (транспорта), сама подпись токеном не является и её
    маскировать не нужно.
    """
    payload = user_id + _WEB_AGENT_SALT_1 + _WEB_AGENT_SALT_2 + str(internal_id)
    digest_hex = hashlib.sha256(payload.encode()).hexdigest()
    return base64.b64encode(digest_hex.encode()).decode()


# --- Критерии поиска собеседника ----------------------------------------

# Допустимые значения строго заглавными — проверено живьём: сервер
# отклоняет всё, кроме "ANY"/"MALE"/"FEMALE", ошибкой 500 при
# десериализации своего перечисления `Sex` (см. докстринг
# `SearchCriteria`). `Literal` не даёт передать неверное значение по
# недосмотру ещё на этапе тайпчека.
Sex = Literal["ANY", "MALE", "FEMALE"]


@dataclass(frozen=True, slots=True)
class AgeRange:
    """Диапазон возраста для критериев поиска (поля "from"/"to" в протоколе)."""

    from_: int
    to: int

    def to_payload(self) -> dict[str, int]:
        """Сериализует в словарь протокола вида {"from": ..., "to": ...}."""
        return {"from": self.from_, "to": self.to}


@dataclass(frozen=True, slots=True)
class SearchCriteria:
    """Критерии поиска собеседника для сообщения "scan-for-peer".

    Значения по умолчанию — самые нейтральные из возможных.

    Пол ("ANY" у себя и у собеседника по умолчанию, "без ограничений по
    полу") проверен и подтверждён живьём: сервер принимает `userSex:
    "ANY"`, `peerSex: "ANY"` без ошибок. Допустимые значения — ровно
    "ANY"/"MALE"/"FEMALE" (заглавными), любое другое сервер отклоняет
    ошибкой 500 при десериализации своего перечисления `Sex`.

    `group=1` по-прежнему подобран ДОПУЩЕНИЕМ: спецификация задачи не
    раскрывает полный список групп (категорий чата) и их смысл. Живой
    прогон показал, что сервер принимает `group` со значениями 1, 2 и 3
    без ошибок, но что именно означает каждая группа, выяснить не
    удалось — до фактического подбора собеседника дело не дошло (см.
    ниже про "captcha-request"), так что смысл групп остаётся
    непроверенным.

    Возрастные диапазоны (`user_age`, `peer_ages`) живьём не проверялись
    вовсе: `None` по умолчанию — по-прежнему ДОПУЩЕНИЕ, что сервис
    трактует отсутствие диапазона как отсутствие ограничения по возрасту.

    Важный факт с живой проверки: в ответ на "scan-for-peer" сервис может
    прислать сообщение "captcha-request" (`captchaType: "RECAPTCHA"`) и
    НЕ поставить клиента в очередь поиска, пока капча не пройдена — см.
    `TYPE_CAPTCHA_REQUEST` и `bot.nekto.events.CaptchaRequiredEvent`.
    Именно из-за этого живая проверка не смогла дойти до реального
    подбора собеседника и проверить смысл `group` и возрастные диапазоны
    до конца.
    """

    group: int = 1
    user_sex: Sex = "ANY"
    peer_sex: Sex = "ANY"
    user_age: AgeRange | None = None
    peer_ages: tuple[AgeRange, ...] | None = None

    def to_payload(self) -> dict[str, Any]:
        """Сериализует в словарь поля "searchCriteria" сообщения "scan-for-peer"."""
        payload: dict[str, Any] = {
            "group": self.group,
            "userSex": self.user_sex,
            "peerSex": self.peer_sex,
        }
        if self.user_age is not None:
            payload["userAge"] = self.user_age.to_payload()
        if self.peer_ages:
            payload["peerAges"] = [age.to_payload() for age in self.peer_ages]
        return payload


# --- Сборка исходящих сообщений -----------------------------------------


def build_register_message(
    token: str, *, timezone: str, locale: str, user_agent: str
) -> dict[str, Any]:
    """Собирает сообщение "register", отправляемое сразу после открытия соединения.

    Признак "firefox" добавляется только если в переданном User-Agent есть
    подстрока "Gecko" — так поступает рабочий клиент; само поле влияет на
    то, как сервис ведёт сигнализацию на своей стороне (детали не
    документированы, проверить нельзя — следуем спецификации буквально).
    """
    message: dict[str, Any] = {
        "type": "register",
        "android": False,
        "version": REGISTER_VERSION,
        "userId": token,
        "timeZone": timezone,
        "locale": locale,
    }
    if "Firefox/" in user_agent:
        message["firefox"] = True
    return message


def build_web_agent_message(signature: str) -> dict[str, Any]:
    """Собирает сообщение "web-agent", завершающее рукопожатие."""
    return {"type": "web-agent", "data": signature}


def build_scan_message(criteria: SearchCriteria) -> dict[str, Any]:
    """Собирает сообщение "scan-for-peer": запуск поиска собеседника."""
    return {
        "type": "scan-for-peer",
        "peerToPeer": True,
        "token": None,
        "searchCriteria": criteria.to_payload(),
    }


def build_stop_scan_message() -> dict[str, Any]:
    """Собирает сообщение "stop-scan": отмена поиска, пока собеседник ещё не найден."""
    return {"type": "stop-scan"}


def build_peer_disconnect_message(connection_id: str) -> dict[str, Any]:
    """Собирает сообщение "peer-disconnect": разрыв уже установленного разговора."""
    return {"type": "peer-disconnect", "connectionId": connection_id}


def build_peer_mute_message(connection_id: str, *, muted: bool) -> dict[str, Any]:
    """Собирает сообщение "peer-mute", которое инициатор шлёт перед offer."""
    return {"type": "peer-mute", "connectionId": connection_id, "muted": muted}


def build_offer_message(connection_id: str, *, sdp: str, type_: str) -> dict[str, Any]:
    """Собирает сообщение "offer".

    Поле "offer" — не вложенный объект, а СТРОКА с JSON вида {"sdp": ...,
    "type": ...} (проверено дампом протокола) — если положить туда словарь
    как есть, сервер сообщение не примет. Не убирайте `json.dumps` как
    «лишнюю» обёртку. `separators=(",", ":")` — без пробелов после
    разделителей, как у браузерного `JSON.stringify`, который их не ставит.
    """
    return {
        "type": "offer",
        "offer": json.dumps({"sdp": sdp, "type": type_}, separators=(",", ":")),
        "connectionId": connection_id,
    }


def build_answer_message(connection_id: str, *, sdp: str, type_: str) -> dict[str, Any]:
    """Собирает сообщение "answer" (строковый JSON внутри поля — см. `build_offer_message`)."""
    return {
        "type": "answer",
        "answer": json.dumps({"sdp": sdp, "type": type_}, separators=(",", ":")),
        "connectionId": connection_id,
    }


def build_stream_received_message(connection_id: str) -> dict[str, Any]:
    """Собирает сообщение "stream-received": подтверждение получения аудиопотока собеседника."""
    return {"type": "stream-received", "connectionId": connection_id}


def build_peer_connection_message(connection_id: str, *, connected: bool) -> dict[str, Any]:
    """Собирает сообщение "peer-connection": уведомление сервиса о состоянии WebRTC-соединения."""
    return {"type": "peer-connection", "connectionId": connection_id, "connection": connected}
