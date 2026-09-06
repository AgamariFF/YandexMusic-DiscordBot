"""Сессия «Моей волны»: буферизация треков и отправка фидбека."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any

from bot.errors import BotError, WaveUnavailableError
from bot.yandex.client import TrackInfo, YandexMusicClient, build_feedback

logger = logging.getLogger(__name__)

RECENT_TRACKS_MEMORY = 50

# Держим ровно один следующий трек: цепочка перезапрашивается после каждого
# выданного трека (см. `_refresh_chain`), поэтому более глубокий запас всё
# равно успевает устареть к моменту, когда до него дойдёт очередь.
MAX_BUFFERED_TRACKS = 1

# Ограничивает очередь неотправленных фидбеков, чтобы при долгой
# недоступности сети (или чужой волне, взявшей паузу надолго) она не росла
# без предела. При переполнении `deque` молча теряет самые старые записи —
# это предпочтительнее падения бота или неограниченного роста памяти.
MAX_PENDING_FEEDBACKS = 20


class WaveSession:
    """Сессия «Моей волны»: буферизует треки цепочкой и копит фидбек для отправки.

    В буфере хранится не больше `MAX_BUFFERED_TRACKS` (один) следующего
    трека — глубже намеренно не буферизуем, так как цепочка всё равно
    обновляется после каждого выданного трека.

    Пропуск трека НЕ пересобирает очередь: следующий трек по-прежнему берётся
    из уже полученной цепочки буфера — ровно тот, что показывался в `/queue`.
    Вместо пересборки, после выдачи каждого трека «хвост» цепочки обновляется
    отдельным запросом (`_refresh_chain`) с учётом фидбека, накопленного по
    этому треку (в том числе skip). Сессионный rotor-API передаёт такой
    фидбек ТЕМ ЖЕ запросом, которым запрашивается цепочка, а не отдельным
    вызовом, — это исключает гонку между ними. Фидбек, который не удалось
    доставить, возвращается в очередь `_pending_feedbacks` и уезжает со
    следующей попыткой, а не теряется. Последние `RECENT_TRACKS_MEMORY`
    выданных треков запоминаются и отфильтровываются из новых пачек, чтобы
    волна не повторяла недавно сыгранное.
    """

    MAX_FETCH_ATTEMPTS = 4
    RETRY_DELAY_SECONDS = 1.0

    def __init__(self, client: YandexMusicClient) -> None:
        """Запоминает клиент и инициализирует пустое состояние сессии."""
        self._client = client
        self._buffer: deque[TrackInfo] = deque()
        self._pending_feedbacks: deque[dict[str, Any]] = deque(maxlen=MAX_PENDING_FEEDBACKS)
        self._batch_id: str | None = None
        self._current_batch_id: str | None = None
        self._last_track: TrackInfo | None = None
        self._recent: deque[str] = deque(maxlen=RECENT_TRACKS_MEMORY)
        self._started = False

    @property
    def started(self) -> bool:
        """Была ли волна запущена."""
        return self._started

    @property
    def buffered(self) -> int:
        """Число треков, ожидающих воспроизведения в буфере."""
        return len(self._buffer)

    @property
    def batch_id(self) -> str | None:
        """Идентификатор пачки, к которой относится последний выданный трек.

        Именно с ним уходит фидбек по треку. Цепочка в буфере обновляется
        отдельно и может уже иметь другой идентификатор.
        """
        return self._current_batch_id if self._current_batch_id is not None else self._batch_id

    async def start(self) -> None:
        """Запускает (или перезапускает) сессию волны и сбрасывает буфер.

        Не занимается доставкой фидбека ПРЕДЫДУЩЕЙ сессии. `WaveSession` —
        не переиспользуемый объект: при каждом перезапуске волны владелец
        (`GuildPlayer.start_wave`) создаёт НОВЫЙ `WaveSession`, так что на
        объекте, у которого вызван этот `start()`, очередь
        `_pending_feedbacks` всегда пуста — слать здесь нечего. А вот у
        СТАРОГО объекта, который сейчас будет выброшен, в очереди мог
        остаться фидбек (например, skip из
        `_interrupt_current_track_locked`), и отправить его можно только
        через сам этот старый объект, пока жива его сессия. Поэтому
        ответственность за сброс фидбека предыдущей сессии лежит на
        вызывающем коде: он обязан вызвать `flush_pending_feedbacks()` у
        СТАРОГО `WaveSession` до того, как создать этот новый (см.
        `GuildPlayer.start_wave` и `GuildPlayer.disconnect`).

        Создание новой сессии сразу возвращает первую пачку треков
        (`includeTracksInResponse`), поэтому, в отличие от классического
        ротора, отдельный запрос за треками сразу после старта не нужен —
        лишний сетевой круг исчезает. Пачка кладётся в буфер как есть: сразу
        после `self._recent.clear()` ниже фильтровать её по `_recent` было
        бы бессмысленно — список только что опустел.
        """
        batch = await self._client.start_session()
        self._buffer.clear()
        self._current_batch_id = None
        self._last_track = None
        self._recent.clear()
        self._batch_id = batch.batch_id
        self._buffer.extend(batch.tracks[:MAX_BUFFERED_TRACKS])
        self._started = True
        logger.info(
            "Сессия «Моей волны» запущена, получено треков: %d, в буфере: %d",
            len(batch.tracks),
            len(self._buffer),
        )

    async def next_track(self) -> TrackInfo:
        """Возвращает следующий трек волны, при необходимости подгружая новую пачку."""
        if not self._started:
            raise WaveUnavailableError(user_message="«Моя волна» не запущена.")

        if not self._buffer:
            queue = self._build_queue(self._last_track) if self._last_track is not None else []

            # Последняя непустая пачка, состоящая из одних повторов: запасной
            # вариант на случай, если свежих треков так и не найдётся.
            repeats_only: tuple[TrackInfo, ...] = ()
            repeats_only_batch_id: str | None = None

            for attempt in range(1, self.MAX_FETCH_ATTEMPTS + 1):
                logger.debug("Попытка %d получить пачку треков волны", attempt)
                feedbacks = self._drain_pending_feedbacks()
                try:
                    batch = await self._client.fetch_session_tracks(
                        queue=queue, feedbacks=feedbacks
                    )
                except WaveUnavailableError as exc:
                    logger.warning(
                        "Попытка %d получить пачку треков волны не удалась: %s: %s",
                        attempt,
                        type(exc).__name__,
                        exc,
                    )
                    self._requeue_feedbacks(feedbacks)
                    batch = None
                except Exception:
                    # Любая другая ошибка (в первую очередь YandexAuthError) —
                    # фатальна и пробрасывается выше без ретраев, но фидбек,
                    # который не успел уйти, всё равно должен вернуться в
                    # очередь, а не потеряться.
                    self._requeue_feedbacks(feedbacks)
                    raise

                if batch is not None and batch.tracks:
                    fresh = tuple(t for t in batch.tracks if t.id not in self._recent)
                    if fresh:
                        repeats = len(batch.tracks) - len(fresh)
                        if repeats:
                            logger.debug("Отфильтровано недавно игравших треков: %d", repeats)
                        self._batch_id = batch.batch_id
                        self._buffer.extend(fresh[:MAX_BUFFERED_TRACKS])
                        break
                    repeats_only = batch.tracks
                    repeats_only_batch_id = batch.batch_id
                if attempt < self.MAX_FETCH_ATTEMPTS:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * 2 ** (attempt - 1))

            if not self._buffer and repeats_only:
                # Свежих треков волна не дала: лучше повтор, чем тишина.
                logger.info(
                    "Волна вернула только недавно игравшие треки, "
                    "проигрываем их повторно, чтобы не прерывать музыку"
                )
                self._batch_id = repeats_only_batch_id
                self._buffer.extend(repeats_only[:MAX_BUFFERED_TRACKS])

            if not self._buffer:
                raise WaveUnavailableError(
                    user_message="Не удалось получить треки «Моей волны». Попробуйте позже."
                )

        track = self._buffer.popleft()
        self._last_track = track
        self._recent.append(track.id)
        # Фидбек по этому треку должен уйти с batch_id той цепочки, из которой
        # он взят, поэтому фиксируем его ДО обновления цепочки ниже.
        self._current_batch_id = self._batch_id
        await self._refresh_chain(track)
        return track

    def upcoming(self, limit: int = 5) -> list[TrackInfo]:
        """Возвращает до `limit` следующих треков буфера, не изменяя его."""
        if limit <= 0:
            return []
        return list(self._buffer)[:limit]

    async def track_started(self, track: TrackInfo) -> None:
        """Кладёт в очередь фидбек о начале воспроизведения трека.

        Фидбек не уходит немедленно отдельным запросом: он остаётся в
        очереди и будет отправлен вместе со следующим запросом пачки треков
        (см. `_refresh_chain`) — именно так работает сессионный rotor-API.
        """
        self._enqueue_feedback(
            build_feedback("trackStarted", batch_id=self.batch_id, track_id=track.feedback_id)
        )

    async def track_finished(self, track: TrackInfo, played_seconds: float) -> None:
        """Кладёт в очередь фидбек о завершении воспроизведения трека."""
        track_length_seconds = track.duration if track.duration > 0 else None
        self._enqueue_feedback(
            build_feedback(
                "trackFinished",
                batch_id=self.batch_id,
                track_id=track.feedback_id,
                total_played_seconds=self._normalize_played_seconds(played_seconds),
                track_length_seconds=track_length_seconds,
            )
        )

    async def track_skipped(self, track: TrackInfo, played_seconds: float) -> None:
        """Кладёт в очередь фидбек о пропуске трека.

        Пропуск, как и раньше, НЕ пересобирает очередь: следующий трек
        по-прежнему берётся из текущей цепочки буфера — того же, что уже
        показывался в `/queue`. А вот способ доставки самого фидбека
        изменился: раньше он уходил немедленно отдельным запросом и мог не
        успеть повлиять на подбор следующей пачки, а теперь остаётся в
        очереди и уезжает ТЕМ ЖЕ запросом, которым запрашивается следующая
        пачка (`_refresh_chain` → `fetch_session_tracks`), — в этом и
        заключается главный смысл перехода на сессионный rotor-API.
        """
        self._enqueue_feedback(
            build_feedback(
                "skip",
                batch_id=self.batch_id,
                track_id=track.feedback_id,
                total_played_seconds=self._normalize_played_seconds(played_seconds),
            )
        )

    async def flush_pending_feedbacks(self) -> None:
        """Немедленно досылает накопленный фидбек, не дожидаясь следующей пачки треков.

        Обычный путь доставки — прицепить фидбек к запросу
        `fetch_session_tracks` (см. `_refresh_chain`), но он работает, только
        пока у этой сессии ЕЩЁ БУДЕТ следующий такой запрос. Когда сессию
        вот-вот забросят — волна перезапускается новым `WaveSession`, либо
        плеер отключается совсем, — следующего запроса не будет никогда, и
        владелец сессии (`GuildPlayer`) обязан вызвать этот метод, пока
        сессия ещё жива, чтобы фидбек не пропал вместе с объектом. Безопасен
        при пустой очереди и не бросает исключений: `client.send_feedbacks`
        сама проглатывает неудачи через `_log_feedback_failure`.
        """
        await self._client.send_feedbacks(self._drain_pending_feedbacks())

    def _enqueue_feedback(self, feedback: dict[str, Any]) -> None:
        """Добавляет фидбек в очередь: он уедет вместе со следующим запросом пачки треков."""
        self._pending_feedbacks.append(feedback)

    def _drain_pending_feedbacks(self) -> list[dict[str, Any]]:
        """Извлекает из очереди все накопленные фидбеки для отправки одним запросом."""
        feedbacks = list(self._pending_feedbacks)
        self._pending_feedbacks.clear()
        return feedbacks

    def _requeue_feedbacks(self, feedbacks: list[dict[str, Any]]) -> None:
        """Возвращает недоставленные фидбеки в начало очереди, сохраняя исходный порядок.

        `extendleft` разворачивает переданную последовательность, поэтому
        расширяем очередь реверснутым списком — иначе фидбеки поменялись бы
        местами между собой.

        Инвариант, на который опирается этот вызов: очередь в момент
        возврата пуста. Её опустошает `_drain_pending_feedbacks`
        непосредственно перед запросом, а все обращения к `WaveSession`
        сериализованы локом плеера (`GuildPlayer._lock`), так что новый
        фидбек не успевает добавиться, пока мы ждём ответ. Это важно:
        `extendleft` на НЕПУСТОМ `deque(maxlen=...)` вытеснял бы записи с
        правого конца — то есть выбрасывал бы самый свежий фидбек, освобождая
        место под возвращаемый старый, что прямо противоположно тому, что
        обещает комментарий у `MAX_PENDING_FEEDBACKS`.
        """
        self._pending_feedbacks.extendleft(reversed(feedbacks))

    def _build_queue(self, after: TrackInfo) -> list[str]:
        """Строит `queue` запроса пачки: составной id `after`, плюс следующий, если он уже в буфере.

        Официальное приложение сообщает серверу оба конца своей локальной
        очереди воспроизведения — трек, по которому едет фидбек, и
        следующий за ним, уже ожидающий в очереди (проверено дампом
        трафика). Раньше мы передавали единственный голый идентификатор.
        Оба элемента — составные `feedback_id` (`<id трека>:<id альбома>`),
        а не `id`.
        """
        queue = [after.feedback_id]
        if self._buffer:
            queue.append(self._buffer[0].feedback_id)
        return queue

    async def _refresh_chain(self, after: TrackInfo) -> None:
        """Обновляет хвост цепочки буфера после выдачи трека `after`.

        Запрашивает `fetch_session_tracks` с `queue`, построенным из `after`
        и (если есть) следующего за ним трека буфера (см. `_build_queue`), и
        всеми фидбеками, накопленными с прошлого запроса (включая фидбек по
        только что выданному треку, в том числе skip), — сервер учитывает их
        В ЭТОМ ЖЕ запросе при подборе следующей пачки, поэтому отдельный
        запрос фидбека не нужен и гонка между ним и получением треков
        исключена. Полученная цепочка ЗАМЕНЯЕТ текущий буфер; сам `after` в
        буфер не возвращается, так как он уже попал в `_recent` и будет
        отфильтрован наравне с прочими недавними треками.

        Ретраев здесь нет: обновление цепочки — это подстройка на будущее,
        а не выдача трека прямо сейчас, поэтому любая ошибка (`BotError`,
        включая `WaveUnavailableError` и `YandexAuthError`) просто
        логируется на уровне warning и оставляет буфер прежним —
        воспроизведение не должно прерываться из-за неудачного обновления.
        Фидбек, который не удалось отправить, возвращается в начало очереди
        и уедет со следующей попыткой (следующим `next_track()` или
        `_refresh_chain`), чтобы не потеряться при сбое.
        """
        feedbacks = self._drain_pending_feedbacks()
        try:
            batch = await self._client.fetch_session_tracks(
                queue=self._build_queue(after), feedbacks=feedbacks
            )
        except BotError as exc:
            self._requeue_feedbacks(feedbacks)
            logger.warning(
                "Не удалось обновить цепочку «Моей волны»: %s: %s", type(exc).__name__, exc
            )
            return

        if not batch.tracks:
            logger.debug("Обновление цепочки «Моей волны» вернуло пустой ответ, буфер не изменён")
            return

        fresh = tuple(t for t in batch.tracks if t.id not in self._recent)
        if not fresh:
            logger.debug(
                "Обновление цепочки «Моей волны» вернуло только недавние треки, буфер не изменён"
            )
            return

        self._buffer.clear()
        self._buffer.extend(fresh[:MAX_BUFFERED_TRACKS])
        self._batch_id = batch.batch_id
        logger.debug("Цепочка «Моей волны» обновлена, в буфере %d треков", len(self._buffer))

    @staticmethod
    def _normalize_played_seconds(played_seconds: float) -> float:
        """Округляет длительность прослушивания и ограничивает её снизу нулём."""
        return float(round(max(0.0, played_seconds)))
