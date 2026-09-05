"""Сессия «Моей волны»: буферизация треков и отправка фидбека."""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from bot.errors import BotError, WaveUnavailableError
from bot.yandex.client import TrackInfo, YandexMusicClient

logger = logging.getLogger(__name__)

RECENT_TRACKS_MEMORY = 50


class WaveSession:
    """Сессия «Моей волны»: буферизует треки цепочкой и отправляет фидбек.

    Пропуск трека НЕ пересобирает очередь: следующий трек по-прежнему берётся
    из уже полученной цепочки буфера — ровно тот, что показывался в `/queue`.
    Вместо пересборки, после выдачи каждого трека «хвост» цепочки обновляется
    отдельным запросом (`_refresh_chain`) с учётом отправленного по этому
    треку фидбека (в том числе skip) — так волна подстраивается под вкус
    пользователя, не выбрасывая уже показанный следующий трек. Последние
    `RECENT_TRACKS_MEMORY` выданных треков запоминаются и отфильтровываются
    из новых пачек, чтобы волна не повторяла недавно сыгранное.
    """

    MAX_FETCH_ATTEMPTS = 4
    RETRY_DELAY_SECONDS = 1.0

    def __init__(self, client: YandexMusicClient) -> None:
        """Запоминает клиент и инициализирует пустое состояние сессии."""
        self._client = client
        self._buffer: deque[TrackInfo] = deque()
        self._batch_id: str | None = None
        self._current_batch_id: str | None = None
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
        """Идентификатор пачки, к которой относится последний выданный трек.

        Именно с ним уходит фидбек по треку. Цепочка в буфере обновляется
        отдельно и может уже иметь другой идентификатор.
        """
        return self._current_batch_id if self._current_batch_id is not None else self._batch_id

    async def start(self) -> None:
        """Запускает (или перезапускает) станцию волны и сбрасывает буфер."""
        await self._client.start_wave()
        self._buffer.clear()
        self._batch_id = None
        self._current_batch_id = None
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
                try:
                    batch = await self._client.fetch_wave_batch(queue=self._last_track_id)
                except WaveUnavailableError as exc:
                    logger.warning(
                        "Попытка %d получить пачку треков волны не удалась: %s: %s",
                        attempt,
                        type(exc).__name__,
                        exc,
                    )
                    batch = None

                if batch is not None and batch.tracks:
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
                    await asyncio.sleep(self.RETRY_DELAY_SECONDS * 2 ** (attempt - 1))

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
        # Фидбек по этому треку должен уйти с batch_id той цепочки, из которой
        # он взят, поэтому фиксируем его ДО обновления цепочки ниже.
        self._current_batch_id = self._batch_id
        await self._refresh_chain(track.id)
        return track

    def upcoming(self, limit: int = 5) -> list[TrackInfo]:
        """Возвращает до `limit` следующих треков буфера, не изменяя его."""
        if limit <= 0:
            return []
        return list(self._buffer)[:limit]

    async def track_started(self, track: TrackInfo) -> None:
        """Сообщает о начале воспроизведения трека."""
        await self._client.notify_track_started(track.id, self.batch_id)

    async def track_finished(self, track: TrackInfo, played_seconds: float) -> None:
        """Сообщает о завершении воспроизведения трека."""
        await self._client.notify_track_finished(
            track.id, self._normalize_played_seconds(played_seconds), self.batch_id
        )

    async def track_skipped(self, track: TrackInfo, played_seconds: float) -> None:
        """Сообщает о пропуске трека.

        Пропуск НЕ пересобирает очередь: следующий трек по-прежнему берётся
        из текущей цепочки буфера — того же, что уже показывался в
        `/queue`. Обновление хвоста цепочки с учётом этого фидбека
        произойдёт отдельно, в `_refresh_chain`, при выдаче следующего
        трека через `next_track()`.
        """
        await self._client.notify_track_skipped(
            track.id, self._normalize_played_seconds(played_seconds), self.batch_id
        )

    async def _refresh_chain(self, after_track_id: str) -> None:
        """Обновляет хвост цепочки буфера после выдачи трека `after_track_id`.

        Согласно протоколу rotor, после отправки фидбека по переданному
        треку следующий запрос `station/tracks` с `queue` = id этого трека
        либо сдвигает цепочку на один элемент, либо возвращает новые
        треки — и то и другое учитывает уже отправленный фидбек (включая
        пропуск). Полученная цепочка ЗАМЕНЯЕТ текущий буфер; сам
        `after_track_id` в буфер не возвращается, так как он уже попал в
        `_recent` и будет отфильтрован наравне с прочими недавними треками.

        Ретраев здесь нет: обновление цепочки — это подстройка на будущее,
        а не выдача трека прямо сейчас, поэтому любая ошибка (`BotError`,
        включая `WaveUnavailableError` и `YandexAuthError`) просто
        логируется на уровне warning и оставляет буфер прежним —
        воспроизведение не должно прерываться из-за неудачного обновления.
        """
        try:
            batch = await self._client.fetch_wave_batch(queue=after_track_id)
        except BotError as exc:
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
        self._buffer.extend(fresh)
        self._batch_id = batch.batch_id
        logger.debug("Цепочка «Моей волны» обновлена, в буфере %d треков", len(self._buffer))

    @staticmethod
    def _normalize_played_seconds(played_seconds: float) -> float:
        """Округляет длительность прослушивания и ограничивает её снизу нулём."""
        return float(round(max(0.0, played_seconds)))
