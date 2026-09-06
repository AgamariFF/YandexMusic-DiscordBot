"""Асинхронная обёртка над неофициальным API Яндекс.Музыки (сессионный rotor «Моя волна»)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from yandex_music import ClientAsync, Track
from yandex_music.exceptions import UnauthorizedError, YandexMusicError

from bot.errors import TrackUnavailableError, WaveUnavailableError, YandexAuthError

logger = logging.getLogger(__name__)

WAVE_STATION_ID = "user:onyourwave"

# Полный список типов фидбека шире (см. docs/rotor-session-api.md), но `like`
# и `dislike` сюда сознательно не включены: бот работает на общем сервере с
# одним аккаунтом Яндекса, и такая оценка осела бы в личной коллекции
# владельца токена, а не отражала бы вкус конкретного слушателя.
FeedbackType = Literal["radioStarted", "trackStarted", "trackFinished", "skip"]


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """Доменное представление трека волны."""

    id: str
    title: str
    artists: str
    duration: float
    raw: Any

    @property
    def display(self) -> str:
        """Строка вида «Артист — Название [3:45]» (без длительности, если она неизвестна)."""
        base = f"{self.artists} — {self.title}"
        if self.duration <= 0:
            return base
        total_seconds = int(self.duration)
        minutes, seconds = divmod(total_seconds, 60)
        return f"{base} [{minutes}:{seconds:02d}]"


@dataclass(frozen=True, slots=True)
class WaveBatch:
    """Пачка треков волны с идентификатором батча для фидбека."""

    batch_id: str | None
    tracks: tuple[TrackInfo, ...]


def build_feedback(
    type_: FeedbackType,
    *,
    batch_id: str | None,
    track_id: str | None = None,
    total_played_seconds: float | None = None,
    track_length_seconds: float | None = None,
    from_: str | None = None,
) -> dict[str, Any]:
    """Собирает один элемент фидбека сессионного rotor в формате, ожидаемом сервером.

    Полезная нагрузка лежит во вложенном объекте `event`, а `batchId` и
    `from` — его СОСЕДИ на верхнем уровне, а не поля самого `event`
    (проверено вживую, см. docs/rotor-session-api.md). Необязательные поля
    (`trackId`, `totalPlayedSeconds`, `trackLengthSeconds`, `batchId`,
    `from`) включаются только когда значение действительно передано — набор
    ключей в точности соответствует тому, что приложение шлёт для
    конкретного типа события, и серверу не уходит лишний явный `null`.
    """
    event: dict[str, Any] = {"type": type_, "timestamp": datetime.now().timestamp()}
    if track_id is not None:
        event["trackId"] = track_id
    if total_played_seconds is not None:
        event["totalPlayedSeconds"] = total_played_seconds
    if track_length_seconds is not None:
        event["trackLengthSeconds"] = track_length_seconds

    feedback: dict[str, Any] = {"event": event}
    if batch_id is not None:
        feedback["batchId"] = batch_id
    if from_ is not None:
        feedback["from"] = from_
    return feedback


class YandexMusicClient:
    """Асинхронная обёртка над неофициальным API Яндекс.Музыки (сессионный rotor «Моя волна»)."""

    def __init__(self, token: str, *, station: str = WAVE_STATION_ID) -> None:
        """Запоминает токен Яндекса и идентификатор станции волны."""
        self._token = token
        self._station = station
        self._client: ClientAsync | None = None
        self._feedback_warned = False
        self._radio_session_id: str | None = None

    @property
    def station(self) -> str:
        """Идентификатор станции rotor, используемой сессией."""
        return self._station

    @property
    def connected(self) -> bool:
        """Признак того, что клиент инициализирован и готов к запросам."""
        return self._client is not None

    async def connect(self) -> None:
        """Создаёт и инициализирует клиент Яндекс.Музыки."""
        logger.info("Подключение к API Яндекс.Музыки")
        client = ClientAsync(self._token)
        try:
            await client.init()
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось авторизоваться в Яндекс.Музыке: %s: %s", type(exc).__name__, exc
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось подключиться к Яндекс.Музыке: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось подключиться к Яндекс.Музыке: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        self._client = client
        logger.info("Подключение к API Яндекс.Музыки установлено")

    def _require_client(self) -> ClientAsync:
        """Возвращает активный клиент либо бросает WaveUnavailableError."""
        if self._client is None:
            raise WaveUnavailableError(
                user_message="«Моя волна» не подключена. Попробуйте позже."
            )
        return self._client

    def _require_session(self) -> str:
        """Возвращает идентификатор активной сессии волны либо бросает WaveUnavailableError.

        Идентификатор появляется только после успешного `start_session()`.
        Вызов метода, которому он нужен, раньше — ошибка состояния
        вызывающей стороны (`WaveSession` обязана вызывать `start_session()`
        первой), но наружу отдаём тот же `WaveUnavailableError`, что и
        `_require_client`, чтобы вызывающий код не различал эти два случая
        недоступности волны.
        """
        if self._radio_session_id is None:
            raise WaveUnavailableError(
                user_message="«Моя волна» не подключена. Попробуйте позже."
            )
        return self._radio_session_id

    async def start_session(self) -> WaveBatch:
        """Создаёт сессию сессионного rotor и возвращает первую пачку треков.

        В сессионном API создание сессии само по себе является стартом
        прослушивания: отдельный фидбек `radioStarted`, который раньше
        отправлял `start_wave()` классического ротора, здесь не нужен и не
        отправляется — сервер уже знает о начале волны из самого факта
        запроса `session/new` (проверено вживую, см.
        docs/rotor-session-api.md).
        """
        client = self._require_client()
        url = f"{client.base_url}/rotor/session/new"
        payload: dict[str, Any] = {"seeds": [self._station], "includeTracksInResponse": True}
        try:
            raw = await client.request.post(url, json=payload)
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось создать сессию «Моей волны»: %s: %s", type(exc).__name__, exc
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось создать сессию «Моей волны»: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось создать сессию «Моей волны»: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc

        session_id = raw.get("radioSessionId") if isinstance(raw, dict) else None
        if not session_id:
            logger.warning("Ответ на создание сессии «Моей волны» не содержит radioSessionId")
            raise WaveUnavailableError()
        self._radio_session_id = session_id

        batch = self._parse_batch(raw, client)
        logger.info("Сессия «Моей волны» создана, треков в первой пачке: %d", len(batch.tracks))
        return batch

    async def fetch_session_tracks(
        self, *, queue: list[str], feedbacks: list[dict[str, Any]]
    ) -> WaveBatch:
        """Запрашивает следующую пачку треков сессии вместе с накопленным фидбеком.

        Это ключевое отличие сессионного rotor от классического: оценки
        уходят не отдельным запросом, а тем же запросом, которым
        запрашивается следующая пачка, поэтому сервер учитывает их при
        подборе. При раздельной отправке гонка неизбежна — пачка может быть
        подобрана раньше, чем доедет скип (см. docs/rotor-session-api.md).
        """
        client = self._require_client()
        session_id = self._require_session()
        url = f"{client.base_url}/rotor/session/{session_id}/tracks"
        payload: dict[str, Any] = {"queue": queue}
        if feedbacks:
            payload["feedbacks"] = feedbacks
        try:
            raw = await client.request.post(url, json=payload)
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось получить пачку треков волны: %s: %s", type(exc).__name__, exc
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось получить пачку треков волны: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось получить пачку треков волны: %s: %s", type(exc).__name__, exc
            )
            raise WaveUnavailableError() from exc

        return self._parse_batch(raw, client)

    async def send_feedbacks(self, feedbacks: list[dict[str, Any]]) -> None:
        """Отправляет пачку накопленных фидбеков одним запросом, без запроса треков.

        Обычный путь доставки фидбека — прицепить его к `fetch_session_tracks`,
        где сервер учтёт оценку при подборе следующей пачки; этот метод
        нужен, когда фидбек нужно отправить сам по себе. Как и для
        отдельного фидбека в классическом роторе, неудача такой отправки не
        считается фатальной (сервер может, например, отклонить фидбек
        ошибкой «condition is not met») и не должна мешать воспроизведению,
        поэтому она не выбрасывается наружу, а логируется через
        `_log_feedback_failure`.
        """
        if not feedbacks:
            return
        try:
            client = self._require_client()
            session_id = self._require_session()
            # Завершающий слэш в пути обязателен: без него сервер обрывает
            # TCP-соединение вместо ответа 404, и это легко принять за
            # сетевую проблему (проверено вживую, см.
            # docs/rotor-session-api.md) — не убирайте его «для красоты».
            url = f"{client.base_url}/rotor/session/{session_id}/feedbacks/"
            await client.request.post(url, json={"feedbacks": feedbacks})
        except Exception as exc:
            self._log_feedback_failure("отправка фидбеков волны", exc)

    def _parse_batch(self, raw: dict[str, Any] | None, client: ClientAsync) -> WaveBatch:
        """Разбирает ответ сессионного rotor (`session/new` или `.../tracks`) в WaveBatch.

        `StationTracksResult.de_json` здесь не подходит — у ответа сессии
        другая форма: элементы `sequence` это словари, а не объекты
        библиотеки, и вложенный трек нужно вручную превратить в объект
        `Track` через `Track.de_json`, иначе `resolve_stream_url` не сможет
        получить у него ссылку на поток (нужен метод
        `get_download_info_async`, которого нет у сырого словаря).
        Проверено вживую, см. docs/rotor-session-api.md.
        """
        if raw is None:
            logger.debug("Получен пустой ответ rotor вместо пачки треков волны")
            return WaveBatch(batch_id=None, tracks=())

        batch_id = raw.get("batchId")
        sequence = raw.get("sequence") or []

        tracks: list[TrackInfo] = []
        for item in sequence:
            raw_track = item.get("track")
            if not raw_track:
                continue
            track = Track.de_json(raw_track, client)
            if track is None or track.available is False:
                continue
            artists = ", ".join(a.name for a in track.artists) or "Неизвестный исполнитель"
            duration = (track.duration_ms / 1000) if track.duration_ms else 0.0
            tracks.append(
                TrackInfo(
                    id=str(track.id),
                    title=track.title,
                    artists=artists,
                    duration=duration,
                    raw=track,
                )
            )

        if tracks:
            logger.info("Получена пачка треков волны: %d шт.", len(tracks))
        else:
            logger.debug("Получена пустая пачка треков волны")
        return WaveBatch(batch_id=batch_id, tracks=tuple(tracks))

    def _log_feedback_failure(self, what: str, exc: Exception) -> None:
        """Логирует неудачу фидбека: первую заметно, последующие — на DEBUG.

        Сервер может отвергать фидбек постоянно (например, ошибкой
        «condition is not met»), а фидбек уходит на каждый трек — warning
        на каждый из них сделал бы лог непригодным для диагностики.
        """
        if self._feedback_warned:
            logger.debug("Не удалось отправить фидбек (%s): %s: %s", what, type(exc).__name__, exc)
            return
        self._feedback_warned = True
        logger.warning(
            "Не удалось отправить фидбек (%s): %s: %s. "
            "Воспроизведению это не мешает; дальнейшие неудачи фидбека — на уровне DEBUG",
            what,
            type(exc).__name__,
            exc,
        )

    async def resolve_stream_url(self, track: TrackInfo) -> str:
        """Возвращает прямую ссылку на аудиопоток лучшего доступного качества."""
        self._require_client()
        try:
            infos = await track.raw.get_download_info_async()
        except UnauthorizedError as exc:
            logger.error(
                "Не удалось получить ссылку на поток трека %s: %s: %s",
                track.id,
                type(exc).__name__,
                exc,
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning(
                "Не удалось получить ссылку на поток трека %s: %s: %s",
                track.id,
                type(exc).__name__,
                exc,
            )
            raise TrackUnavailableError() from exc
        except OSError as exc:
            logger.warning(
                "Не удалось получить ссылку на поток трека %s: %s: %s",
                track.id,
                type(exc).__name__,
                exc,
            )
            raise TrackUnavailableError() from exc

        candidates = [info for info in infos if not info.preview]
        if not candidates:
            raise TrackUnavailableError()

        mp3_candidates = [info for info in candidates if info.codec == "mp3"]
        pool = mp3_candidates or candidates
        best = max(pool, key=lambda info: info.bitrate_in_kbps or 0)

        logger.debug(
            "Выбран поток трека %s: codec=%s bitrate=%s", track.id, best.codec, best.bitrate_in_kbps
        )

        try:
            return await best.get_direct_link_async()
        except UnauthorizedError as exc:
            # Текст исключения может содержать подписанную ссылку — логируем только тип.
            logger.error(
                "Не удалось получить прямую ссылку на трек %s: %s", track.id, type(exc).__name__
            )
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            # Текст исключения может содержать подписанную ссылку — логируем только тип.
            logger.warning(
                "Не удалось получить прямую ссылку на трек %s: %s", track.id, type(exc).__name__
            )
            raise TrackUnavailableError() from exc
        except OSError as exc:
            # Текст исключения может содержать подписанную ссылку — логируем только тип.
            logger.warning(
                "Не удалось получить прямую ссылку на трек %s: %s", track.id, type(exc).__name__
            )
            raise TrackUnavailableError() from exc

    async def close(self) -> None:
        """Идемпотентно освобождает внутренний клиент."""
        if self._client is None:
            return
        try:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close()
        except Exception:
            logger.warning("Ошибка при закрытии клиента Яндекс.Музыки", exc_info=True)
        finally:
            self._client = None
