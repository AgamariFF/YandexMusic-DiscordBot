"""Асинхронная обёртка над неофициальным API Яндекс.Музыки (сессионный rotor «Моя волна»)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from yandex_music import ClientAsync, Track
from yandex_music.exceptions import UnauthorizedError, YandexMusicError

from bot.errors import (
    SearchUnavailableError,
    TrackUnavailableError,
    WaveUnavailableError,
    YandexAuthError,
)

logger = logging.getLogger(__name__)

WAVE_STATION_ID = "user:onyourwave"

# Размер обложки, подставляемый вместо `%%` в шаблон `cover_uri`/`og_image`
# (например, `avatars.yandex.net/get-music-content/.../%%`) методами
# `get_cover_url`/`get_og_image_url` библиотеки — из самого кода это не
# видно. 400×400 достаточно для крупного показа обложки в сообщении бота и
# не тянет лишний трафик (проверено вживую: `get_cover_url("400x400")`
# отвечает `200 image/jpeg`).
COVER_URL_SIZE = "400x400"

# Значение поля `from` — перечислимая метка контекста запуска, а не
# свободный текст: сервер, судя по всему, её разбирает и учитывает при
# подборе (иначе незачем было бы вообще передавать). Проверить со стороны
# клиента, какие значения сервер понимает, а какие молча относит к
# «неопознанному источнику», нельзя — тело ответа не различается, везде
# `200`. Поэтому здесь ровно та же строка, что шлёт официальное приложение
# (проверено дампом трафика), а не собственная выдумка: если бы фидбек с
# посторонним `from` тихо не учитывался при подборе волны, это была бы ровно
# та невидимая снаружи поломка, ради починки которой затевался весь этот
# инкремент.
FEEDBACK_FROM = "desktop-wave_landing_screen-my_wave-radio-default"

# Полный список типов фидбека шире (см. docs/rotor-session-api.md), но `like`
# и `dislike` сюда сознательно не включены: бот работает на общем сервере с
# одним аккаунтом Яндекса, и такая оценка осела бы в личной коллекции
# владельца токена, а не отражала бы вкус конкретного слушателя. `radioStarted`
# тоже сюда не входит: у него особая форма (`from` дублируется внутри `event`,
# `batchId` не передаётся вовсе), и собирает его отдельная функция
# `build_radio_started_feedback`, а не `build_feedback`.
FeedbackType = Literal["trackStarted", "trackFinished", "skip"]


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """Доменное представление трека волны."""

    id: str
    # Составной `<id трека>:<id альбома>`, ровно в этом виде трек уходит на
    # сервер в `trackId` фидбека и в `queue` запроса пачки (проверено дампом
    # трафика официального приложения: трек 38077233 с альбомом 4849007
    # уходит как "38077233:4849007"). У `id` семантика другая — по нему
    # ведётся внутренняя логика (`_recent`, сравнения), её не трогаем.
    feedback_id: str
    title: str
    artists: str
    duration: float
    # Ссылка на обложку трека (для показа в сообщении бота). `None`, если у
    # трека нет ни `cover_uri`, ни запасного `og_image` — такое редко, но
    # встречается, и падать из-за отсутствия обложки нельзя.
    cover_url: str | None
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


def _now_iso() -> str:
    """Текущее время в UTC как строка ISO-8601 с миллисекундами и суффиксом `Z`.

    Официальное приложение шлёт временные метки вида
    `"2026-09-06T18:32:50.733Z"` (проверено дампом трафика). Стандартный
    `datetime.now(UTC).isoformat()` даёт микросекунды и суффикс `+00:00` —
    ни то ни другое не совпадает: обрезаем дробную часть до миллисекунд
    через `timespec` и вручную заменяем `+00:00` на `Z`, потому что сам
    `datetime` такой суффикс не производит.
    """
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_feedback(
    type_: FeedbackType,
    *,
    batch_id: str | None,
    track_id: str | None = None,
    total_played_seconds: float | None = None,
    track_length_seconds: float | None = None,
    from_: str = FEEDBACK_FROM,
) -> dict[str, Any]:
    """Собирает один элемент фидбека сессионного rotor в формате, ожидаемом сервером.

    Полезная нагрузка лежит во вложенном объекте `event`, а `batchId` и
    `from` — его СОСЕДИ на верхнем уровне, а не поля самого `event`
    (проверено дампом трафика официального приложения, см.
    docs/rotor-session-api.md). Необязательные поля (`trackId`,
    `totalPlayedSeconds`, `trackLengthSeconds`, `batchId`) включаются только
    когда значение действительно передано, а вот `from` уходит всегда —
    приложение кладёт его в каждый фидбек без исключений. `track_id` здесь
    ожидает уже готовый составной идентификатор (`TrackInfo.feedback_id`,
    `<id трека>:<id альбома>`), а не голый `TrackInfo.id`, — именно в таком
    виде сервер хочет видеть `trackId` (проверено дампом трафика).

    `radioStarted` этой функцией не собрать: `FeedbackType` его не включает
    намеренно, потому что у него другая форма (`from` дублируется внутри
    `event`, `batchId` не передаётся вовсе) — для него есть отдельная
    `build_radio_started_feedback`.
    """
    event: dict[str, Any] = {"type": type_, "timestamp": _now_iso()}
    if track_id is not None:
        event["trackId"] = track_id
    if total_played_seconds is not None:
        event["totalPlayedSeconds"] = total_played_seconds
    if track_length_seconds is not None:
        event["trackLengthSeconds"] = track_length_seconds

    feedback: dict[str, Any] = {"event": event, "from": from_}
    if batch_id is not None:
        feedback["batchId"] = batch_id
    return feedback


def build_radio_started_feedback(from_: str = FEEDBACK_FROM) -> dict[str, Any]:
    """Собирает фидбек `radioStarted`, отправляемый отдельным запросом после `session/new`.

    Его форма выбивается из общей схемы `build_feedback`: `batchId` не
    передаётся вовсе (пачка ещё не запрашивалась отдельным запросом
    фидбека), а `from` дублируется — и внутри `event`, и снаружи него.
    Именно так делает официальное приложение сразу после создания сессии
    (проверено дампом трафика), хотя раньше мы считали, что отдельный
    `radioStarted` не нужен вовсе.
    """
    event = {"type": "radioStarted", "timestamp": _now_iso(), "from": from_}
    return {"event": event, "from": from_}


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

    async def start_session(
        self, *, seeds: list[str] | None = None, track_to_start_from: str | None = None
    ) -> WaveBatch:
        """Создаёт сессию сессионного rotor и возвращает первую пачку треков.

        Раньше мы считали, что создание сессии само по себе уже является
        стартом прослушивания, и отдельный `radioStarted` не отправляли. Дамп
        трафика официального приложения показал обратное: оно всё равно шлёт
        `radioStarted` отдельным запросом сразу после `session/new` (см.
        docs/rotor-session-api.md), и мы делаем так же — см. отправку ниже.

        `seeds` не передан — берём `[self._station]`, то есть прежнее
        поведение «Моей волны» от станции пользователя. Для волны от
        конкретного трека вызывающая сторона передаёт `seeds=["track:<id
        трека>"]` и одновременно `track_to_start_from` — это ОТДЕЛЬНОЕ поле
        `trackToStartFrom` тела запроса, а не часть `seeds`. Его роль
        принципиальна и неочевидна из кода: проверено вживую, что именно
        `trackToStartFrom` заставляет запрошенный трек стать ПЕРВЫМ в выдаче
        сессии — без него волна стартует с похожего, но другого трека.
        Официальное приложение этого поля вообще не использует (оно
        проигрывает выбранный трек локально, а волну запускает уже отдельно
        от станции, см. docs/rotor-session-api.md) — не удаляйте параметр как
        «неофициальный» или «лишний»: именно через него бот получает связку
        «сначала сам выбранный трек, потом волна от него» одним запросом,
        без отдельного локального воспроизведения трека вне волны.
        """
        client = self._require_client()
        url = f"{client.base_url}/rotor/session/new"
        payload: dict[str, Any] = {
            "seeds": seeds if seeds is not None else [self._station],
            "includeTracksInResponse": True,
            "includeWaveModel": True,
            "interactive": True,
        }
        if track_to_start_from is not None:
            payload["trackToStartFrom"] = track_to_start_from
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

        # Официальное приложение шлёт `radioStarted` отдельным запросом на
        # `.../feedback/` (в единственном числе) сразу после `session/new`, а
        # не пачкой на `.../feedbacks/` (проверено дампом трафика) —
        # воспроизводим это поведение через `send_feedback`, а не
        # `send_feedbacks`. Неудача этого фидбека не должна ломать запуск
        # волны: `send_feedback` проглатывает такие ошибки через
        # `_log_feedback_failure`, сессия и первая пачка треков к этому
        # моменту уже получены.
        await self.send_feedback(build_radio_started_feedback())

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

    async def search_tracks(self, query: str, *, limit: int = 10) -> tuple[TrackInfo, ...]:
        """Ищет треки по текстовому запросу и возвращает не больше `limit` штук.

        Использует поиск библиотеки (`client.search(..., type_="track")`),
        а не собственный запрос к rotor-API — поиск треков никак не связан с
        сессией волны. Результаты лежат в `result.tracks.results`, но само
        поле `tracks` у ответа опционально (сервер может не вернуть блок
        треков вовсе, например при полностью пустой выдаче) — в этом случае
        просто возвращаем пустой кортеж, а не падаем. Проверено вживую: поиск
        находит треки даже при опечатках в запросе (сервер сам их исправляет,
        см. `nocorrect` у `Search`), отдельная обработка опечаток не нужна.
        """
        client = self._require_client()
        try:
            result = await client.search(query, type_="track")
        except UnauthorizedError as exc:
            logger.error("Не удалось выполнить поиск треков: %s: %s", type(exc).__name__, exc)
            raise YandexAuthError() from exc
        except YandexMusicError as exc:
            logger.warning("Не удалось выполнить поиск треков: %s: %s", type(exc).__name__, exc)
            raise SearchUnavailableError() from exc
        except OSError as exc:
            logger.warning("Не удалось выполнить поиск треков: %s: %s", type(exc).__name__, exc)
            raise SearchUnavailableError() from exc

        if result is None or result.tracks is None:
            logger.debug("Поиск треков не вернул результатов")
            return ()

        tracks: list[TrackInfo] = []
        for track in result.tracks.results:
            info = self._track_info_from_track(track)
            if info is None:
                continue
            tracks.append(info)
            if len(tracks) >= limit:
                break

        logger.info("Поиск треков завершён, найдено: %d шт.", len(tracks))
        return tuple(tracks)

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

    async def send_feedback(self, feedback: dict[str, Any]) -> None:
        """Отправляет один фидбек одиночным запросом на `.../feedback/` (без обёртки списком).

        Это отдельный от `send_feedbacks` маршрут — единственное число в
        пути, тело запроса это сам объект фидбека, а не `{"feedbacks": [...]}`
        вокруг списка. Нужен ровно для одного случая: `radioStarted`, который
        официальное приложение шлёт именно так, отдельным запросом сразу
        после `session/new` (проверено дампом трафика), а не пачкой вместе с
        другими фидбеками. Как и `send_feedbacks`, неудача не должна мешать
        запуску волны, поэтому ошибка не пробрасывается наружу, а логируется
        через `_log_feedback_failure`.
        """
        try:
            client = self._require_client()
            session_id = self._require_session()
            # Завершающий слэш обязателен по той же причине, что и у
            # `.../feedbacks/` в `send_feedbacks`: без него сервер обрывает
            # TCP-соединение вместо ответа 404 (проверено вживую).
            url = f"{client.base_url}/rotor/session/{session_id}/feedback/"
            await client.request.post(url, json=feedback)
        except Exception as exc:
            self._log_feedback_failure("отправка фидбека волны", exc)

    @staticmethod
    def _track_info_from_track(track: Track | None) -> TrackInfo | None:
        """Собирает TrackInfo из объекта Track библиотеки либо возвращает None.

        Общий хелпер для `_parse_batch` (где `Track` собирается вручную из
        сырого словаря пачки rotor через `Track.de_json`) и `search_tracks`
        (где `Track` уже приходит готовым объектом от библиотеки) — правила
        сборки не должны разъезжаться между этими двумя путями: пропуск
        недоступных треков (`available is False`), склейка артистов через
        запятую, составной `feedback_id` вида `<id трека>:<id альбома>`, как
        шлёт официальное приложение (проверено дампом трафика: трек 38077233
        с альбомом 4849007 уходит как "38077233:4849007"), и ссылка на
        обложку. Трек без альбомов теоретически возможен — запасной вариант
        на голый `id` обязателен, падать здесь нельзя.

        `Track.get_cover_url` падает `AssertionError`, если у трека нет
        `cover_uri` (внутри метода — `assert isinstance(self.cover_uri, str)`),
        а это не доменная ошибка и наружу лететь не должно, поэтому наличие
        `cover_uri` проверяем сами. При его отсутствии пробуем запасной
        `og_image` (по данным разведки эти поля обычно совпадают) через тот
        же `COVER_URL_SIZE`; если нет и его — обложки у трека действительно
        нет, и `cover_url` остаётся `None`.
        """
        if track is None or track.available is False:
            return None
        artists = ", ".join(a.name for a in track.artists) or "Неизвестный исполнитель"
        duration = (track.duration_ms / 1000) if track.duration_ms else 0.0
        album_id = track.albums[0].id if track.albums else None
        feedback_id = f"{track.id}:{album_id}" if album_id is not None else str(track.id)
        cover_url: str | None = None
        if isinstance(track.cover_uri, str):
            cover_url = track.get_cover_url(COVER_URL_SIZE)
        elif isinstance(track.og_image, str):
            cover_url = track.get_og_image_url(COVER_URL_SIZE)
        return TrackInfo(
            id=str(track.id),
            feedback_id=feedback_id,
            title=track.title,
            artists=artists,
            duration=duration,
            cover_url=cover_url,
            raw=track,
        )

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
            info = self._track_info_from_track(track)
            if info is None:
                continue
            tracks.append(info)

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
