"""Сессия «Моей волны»: буферизация треков и отправка фидбека."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from bot.errors import WaveUnavailableError
from bot.yandex.client import TrackInfo, YandexMusicClient

logger = logging.getLogger(__name__)

RECENT_TRACKS_MEMORY = 50


class WaveSession:
    """Сессия «Моей волны»: буферизует треки пачками и отправляет фидбек.

    При пропуске трека буфер сбрасывается, и следующая пачка запрашивается
    заново с учётом отправленного фидбека — так волна подстраивается под
    пропуски, как в оригинальном приложении. Последние `RECENT_TRACKS_MEMORY`
    выданных треков запоминаются и отфильтровываются из новых пачек, чтобы
    волна не повторяла недавно сыгранное.
    """

    MAX_FETCH_ATTEMPTS = 3
    RETRY_DELAY_SECONDS = 1.0

    def __init__(self, client: YandexMusicClient) -> None:
        """Запоминает клиент и инициализирует пустое состояние сессии."""
        self._client = client
        self._buffer: deque[TrackInfo] = deque()
        self._batch_id: str | None = None
        self._last_track_id: str | None = None
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
        """Идентификатор текущей пачки треков."""
        return self._batch_id

    async def start(self) -> None:
        """Запускает (или перезапускает) станцию волны и сбрасывает буфер."""
        await self._client.start_wave()
        self._buffer.clear()
        self._batch_id = None
        self._last_track_id = None
        self._recent.clear()
        self._started = True
        logger.info("Сессия «Моей волны» запущена")

    async def next_track(self) -> TrackInfo:
        """Возвращает следующий трек волны, при необходимости подгружая новую пачку."""
        if not self._started:
            raise WaveUnavailableError(user_message="«Моя волна» не запущена.")

        if not self._buffer:
            # Последняя непустая пачка, состоящая из одних повторов: запасной
            # вариант на случай, если свежих треков так и не найдётся.
            repeats_only: tuple[TrackInfo, ...] = ()
            repeats_only_batch_id: str | None = None

            for attempt in range(1, self.MAX_FETCH_ATTEMPTS + 1):
                logger.debug("Попытка %d получить пачку треков волны", attempt)
                batch = await self._client.fetch_wave_batch(queue=self._last_track_id)
                if batch.tracks:
                    fresh = tuple(t for t in batch.tracks if t.id not in self._recent)
                    if fresh:
                        repeats = len(batch.tracks) - len(fresh)
                        if repeats:
                            logger.debug("Отфильтровано недавно игравших треков: %d", repeats)
                        self._batch_id = batch.batch_id
                        self._buffer.extend(fresh)
                        break
                    repeats_only = batch.tracks
                    repeats_only_batch_id = batch.batch_id
                if attempt < self.MAX_FETCH_ATTEMPTS:
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS)

            if not self._buffer and repeats_only:
                # Свежих треков волна не дала: лучше повтор, чем тишина.
                logger.info(
                    "Волна вернула только недавно игравшие треки, "
                    "проигрываем их повторно, чтобы не прерывать музыку"
                )
                self._batch_id = repeats_only_batch_id
                self._buffer.extend(repeats_only)

            if not self._buffer:
                raise WaveUnavailableError(
                    user_message="Не удалось получить треки «Моей волны». Попробуйте позже."
                )

        track = self._buffer.popleft()
        self._last_track_id = track.id
        self._recent.append(track.id)
        return track

    def upcoming(self, limit: int = 5) -> list[TrackInfo]:
        """Возвращает до `limit` следующих треков буфера, не изменяя его."""
        if limit <= 0:
            return []
        return list(self._buffer)[:limit]

    async def track_started(self, track: TrackInfo) -> None:
        """Сообщает о начале воспроизведения трека."""
        await self._client.notify_track_started(track.id, self._batch_id)

    async def track_finished(self, track: TrackInfo, played_seconds: float) -> None:
        """Сообщает о завершении воспроизведения трека."""
        await self._client.notify_track_finished(
            track.id, self._normalize_played_seconds(played_seconds), self._batch_id
        )

    async def track_skipped(self, track: TrackInfo, played_seconds: float) -> None:
        """Сообщает о пропуске трека и сбрасывает буфер, чтобы волна подстроилась.

        Порядок важен: сначала уходит фидбек, и только затем очищается буфер.
        Следующий `next_track()` запросит свежую пачку с `queue` = id
        пропущенного трека, поэтому сервер перестроит волну уже с учётом
        пропуска — так же, как оригинальное приложение.
        """
        await self._client.notify_track_skipped(
            track.id, self._normalize_played_seconds(played_seconds), self._batch_id
        )
        discarded = len(self._buffer)
        self._buffer.clear()
        logger.info(
            "Буфер «Моей волны» сброшен после пропуска трека (отброшено %d треков)",
            discarded,
        )

    @staticmethod
    def _normalize_played_seconds(played_seconds: float) -> float:
        """Округляет длительность прослушивания и ограничивает её снизу нулём."""
        return float(round(max(0.0, played_seconds)))
