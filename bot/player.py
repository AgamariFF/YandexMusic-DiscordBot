"""Плеер «Моей волны» для одного голосового сервера Discord."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

import discord

from bot.audio import BassLevel, TrackedAudioSource, create_source
from bot.errors import (
    BotError,
    NotConnectedError,
    NothingPlayingError,
    TrackUnavailableError,
    VoiceConnectError,
    WaveUnavailableError,
    YandexAuthError,
)
from bot.yandex import TrackInfo, WaveSession, YandexMusicClient

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_TRACK_FAILURES = 5
_IDLE_CHECK_INTERVAL = 5.0


class PlayerState(StrEnum):
    """Состояние плеера."""

    IDLE = "idle"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class NowPlaying:
    """Снимок текущего состояния воспроизведения."""

    track: TrackInfo
    elapsed: float
    volume: float
    bass: BassLevel
    paused: bool


class GuildPlayer:
    """Плеер одного сервера: «Моя волна», один трек за раз."""

    def __init__(
        self,
        client: YandexMusicClient,
        *,
        ffmpeg_path: str = "ffmpeg",
        default_volume: float = 0.5,
        idle_timeout: int = 300,
        announce: Callable[[TrackInfo], Awaitable[None]] | None = None,
    ) -> None:
        """Создаёт плеер сервера с заданными настройками звука и оповещений."""
        self._client = client
        self._ffmpeg_path = ffmpeg_path
        self._volume = max(0.0, min(2.0, default_volume))
        self._bass = BassLevel.OFF
        self._idle_timeout = idle_timeout
        self._announce = announce

        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._voice_client: discord.VoiceClient | None = None
        self._channel: discord.VoiceChannel | discord.StageChannel | None = None
        self._session: WaveSession | None = None
        self._current_track: TrackInfo | None = None
        self._source: TrackedAudioSource | None = None
        self._state = PlayerState.IDLE

        self._consecutive_failures = 0

        self._idle_task: asyncio.Task[None] | None = None
        self._idle_since: float | None = None

    @property
    def state(self) -> PlayerState:
        """Текущее состояние плеера."""
        return self._state

    @property
    def current(self) -> TrackInfo | None:
        """Трек, загруженный сейчас (играет или на паузе), либо None."""
        return self._current_track

    @property
    def volume(self) -> float:
        """Текущая громкость (0.0..2.0), применяемая к активному и будущим трекам."""
        return self._volume

    @property
    def bass(self) -> BassLevel:
        """Текущий уровень бас-буста."""
        return self._bass

    @property
    def voice_client(self) -> discord.VoiceClient | None:
        """Активное голосовое соединение сервера либо None."""
        return self._voice_client

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        """Голосовой канал, к которому подключён плеер, либо None."""
        return self._channel

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Подключается к каналу либо переходит в него, если уже подключён к другому."""
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            try:
                if self._voice_client is not None and self._voice_client.is_connected():
                    await self._voice_client.move_to(channel)
                else:
                    self._voice_client = await channel.connect()
            except (TimeoutError, discord.ClientException, discord.opus.OpusNotLoaded) as exc:
                raise VoiceConnectError(
                    f"Не удалось подключиться к голосовому каналу {channel.id}: {exc}",
                    user_message="Не удалось подключиться к голосовому каналу.",
                ) from exc
            self._channel = channel
            self._restart_idle_timer_locked()

    async def disconnect(self) -> None:
        """Отключается от голосового канала и сбрасывает состояние. Идемпотентен."""
        async with self._lock:
            await self._cancel_idle_timer_locked()
            self._stop_playback_locked()
            self._stop_locked_state()
            self._consecutive_failures = 0
            if self._voice_client is not None:
                try:
                    await self._voice_client.disconnect(force=True)
                except Exception:
                    logger.warning("Ошибка при отключении от голосового канала", exc_info=True)
            self._voice_client = None
            self._channel = None

    async def start_wave(self) -> TrackInfo:
        """Запускает «Мою волну» заново и начинает воспроизведение первого трека."""
        async with self._lock:
            if self._voice_client is None or not self._voice_client.is_connected():
                raise NotConnectedError()

            if self._current_track is not None:
                await self._interrupt_current_track_locked()

            session = WaveSession(self._client)
            await session.start()
            self._session = session
            self._consecutive_failures = 0

            track = await self._advance_locked()
            if track is None:
                raise WaveUnavailableError(user_message="«Моя волна» сейчас недоступна.")
            return track

    async def skip(self) -> TrackInfo | None:
        """Пропускает текущий трек и переходит к следующему в волне."""
        async with self._lock:
            if self._current_track is None or self._voice_client is None:
                raise NothingPlayingError()
            if not (self._voice_client.is_playing() or self._voice_client.is_paused()):
                raise NothingPlayingError()

            track = self._current_track
            session = self._session
            elapsed = self._source.elapsed if self._source is not None else 0.0
            self._stop_playback_locked()
            self._current_track = None

            if session is not None:
                await session.track_skipped(track, elapsed)

            return await self._advance_locked()

    def pause(self) -> None:
        """Ставит текущее воспроизведение на паузу."""
        if self._voice_client is None:
            raise NotConnectedError()
        if self._current_track is None or not self._voice_client.is_playing():
            raise NothingPlayingError()
        self._voice_client.pause()
        self._state = PlayerState.PAUSED

    def resume(self) -> None:
        """Снимает текущее воспроизведение с паузы."""
        if self._voice_client is None:
            raise NotConnectedError()
        if self._current_track is None or not self._voice_client.is_paused():
            raise NothingPlayingError()
        self._voice_client.resume()
        self._state = PlayerState.PLAYING

    def set_volume(self, value: float) -> float:
        """Клампит громкость в 0.0..2.0, применяет её немедленно и возвращает итоговое значение."""
        clamped = max(0.0, min(2.0, value))
        self._volume = clamped
        if self._source is not None:
            self._source.volume = clamped
        return clamped

    async def set_bass(self, level: BassLevel) -> None:
        """Меняет уровень бас-буста; при активном треке перезапускает его с той же позиции."""
        async with self._lock:
            self._bass = level
            if self._current_track is None or self._voice_client is None:
                return

            track = self._current_track
            elapsed = self._source.elapsed if self._source is not None else 0.0
            was_paused = self._state is PlayerState.PAUSED

            self._stop_playback_locked()
            try:
                await self._play_track_locked(track, seek=elapsed, notify=False)
            except BotError as exc:
                logger.error(
                    "Не удалось перезапустить трек %s с новым уровнем баса: %s", track.id, exc
                )
                self._stop_locked_state()
                return

            if was_paused:
                self._voice_client.pause()
                self._state = PlayerState.PAUSED

    def now_playing(self) -> NowPlaying:
        """Возвращает снимок состояния воспроизведения текущего трека."""
        if self._current_track is None:
            raise NothingPlayingError()
        elapsed = self._source.elapsed if self._source is not None else 0.0
        return NowPlaying(
            track=self._current_track,
            elapsed=elapsed,
            volume=self._volume,
            bass=self._bass,
            paused=self._state is PlayerState.PAUSED,
        )

    def queue_preview(self, limit: int = 5) -> list[TrackInfo]:
        """Возвращает до `limit` ближайших треков волны без изменения очереди."""
        if self._session is None:
            return []
        return self._session.upcoming(limit)

    def _after_playback(
        self, source: TrackedAudioSource, error: BaseException | None
    ) -> None:
        """Callback discord.py из потока аудио-плеера: планирует продолжение в event loop."""
        if error is not None:
            logger.error("Ошибка воспроизведения аудио: %s", error)
        loop = self._loop
        if loop is None:
            return
        future = asyncio.run_coroutine_threadsafe(
            self._handle_playback_finished(source), loop
        )
        future.add_done_callback(self._log_playback_finished_result)

    @staticmethod
    def _log_playback_finished_result(future: asyncio.Future[None]) -> None:
        """Логирует исключение из обработки завершения трека, если оно возникло."""
        try:
            future.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ошибка при обработке завершения воспроизведения трека")

    async def _handle_playback_finished(self, source: TrackedAudioSource) -> None:
        """Обрабатывает завершение колбэка конкретного источника: фидбек и переход дальше."""
        async with self._lock:
            if source is not self._source:
                # Колбэк от уже заменённого/остановленного источника — не наш случай.
                return

            track = self._current_track
            session = self._session
            elapsed = source.elapsed
            self._current_track = None
            self._source = None

            if track is not None and session is not None:
                await session.track_finished(track, elapsed)

            await self._advance_locked()

    async def _advance_locked(self) -> TrackInfo | None:
        """Получает и запускает следующий трек волны; при неустранимых ошибках останавливается."""
        if self._session is None:
            self._stop_locked_state()
            return None

        while True:
            try:
                track = await self._session.next_track()
            except (YandexAuthError, WaveUnavailableError):
                logger.exception("«Моя волна» недоступна, воспроизведение остановлено")
                self._stop_locked_state()
                return None

            try:
                await self._play_track_locked(track)
            except TrackUnavailableError:
                self._consecutive_failures += 1
                logger.warning(
                    "Трек %s недоступен (%d/%d подряд), пробуем следующий",
                    track.id,
                    self._consecutive_failures,
                    MAX_CONSECUTIVE_TRACK_FAILURES,
                )
                if self._consecutive_failures >= MAX_CONSECUTIVE_TRACK_FAILURES:
                    logger.error(
                        "Подряд %d недоступных треков волны, воспроизведение остановлено",
                        self._consecutive_failures,
                    )
                    self._stop_locked_state()
                    return None
                continue
            except BotError:
                logger.exception(
                    "Не удалось запустить трек %s, воспроизведение остановлено", track.id
                )
                self._stop_locked_state()
                return None
            else:
                self._consecutive_failures = 0
                return track

    async def _play_track_locked(
        self, track: TrackInfo, *, seek: float = 0.0, notify: bool = True
    ) -> None:
        """Резолвит поток трека и запускает его воспроизведение с указанной позиции."""
        if self._voice_client is None:
            raise NotConnectedError()

        url = await self._client.resolve_stream_url(track)
        try:
            source = create_source(
                url,
                volume=self._volume,
                bass=self._bass,
                ffmpeg_path=self._ffmpeg_path,
                seek=seek,
            )
            self._voice_client.play(
                source, after=functools.partial(self._after_playback, source)
            )
        except (discord.ClientException, discord.opus.OpusNotLoaded) as exc:
            raise VoiceConnectError(
                f"Не удалось начать воспроизведение трека {track.id}: {exc}",
                user_message="Не удалось начать воспроизведение в голосовом канале.",
            ) from exc
        except BotError:
            raise
        except Exception as exc:
            raise TrackUnavailableError(
                f"Не удалось создать источник аудио для трека {track.id}: {exc}"
            ) from exc

        self._current_track = track
        self._source = source
        self._state = PlayerState.PLAYING
        self._restart_idle_timer_locked()

        if notify and self._session is not None:
            await self._session.track_started(track)

        if notify and self._announce is not None:
            try:
                await self._announce(track)
            except Exception:
                logger.exception("Ошибка в колбэке анонса трека %s", track.id)

    async def _interrupt_current_track_locked(self) -> None:
        """Останавливает играющий трек и отправляет по нему фидбек skip перед перезапуском волны."""
        track = self._current_track
        session = self._session
        elapsed = self._source.elapsed if self._source is not None else 0.0
        self._stop_playback_locked()
        if track is not None and session is not None:
            await session.track_skipped(track, elapsed)
        self._current_track = None

    def _stop_playback_locked(self) -> None:
        """Останавливает активное воспроизведение и снимает identity текущего источника.

        Сброс `self._source` до вызова `stop()` гарантирует, что колбэк, который
        придёт по уже остановленному источнику, распознает себя как чужой
        (identity-проверка в `_handle_playback_finished`) и не продвинет очередь
        повторно.
        """
        if self._voice_client is None:
            return
        self._source = None
        if self._voice_client.is_playing() or self._voice_client.is_paused():
            self._voice_client.stop()

    def _stop_locked_state(self) -> None:
        """Сбрасывает состояние воспроизведения и волну, не трогая голосовое соединение."""
        self._current_track = None
        self._source = None
        self._session = None
        self._state = PlayerState.IDLE

    def _channel_is_empty_locked(self) -> bool:
        """Проверяет, что в голосовом канале не осталось людей (только боты либо никого)."""
        if self._channel is None:
            return True
        return not any(not member.bot for member in self._channel.members)

    def _restart_idle_timer_locked(self) -> None:
        """Сбрасывает отсчёт бездействия и при необходимости запускает фоновую задачу."""
        self._idle_since = None
        if self._idle_timeout <= 0:
            return
        if self._idle_task is None or self._idle_task.done():
            loop = self._loop
            if loop is None:
                return
            self._idle_task = loop.create_task(self._idle_watchdog())

    async def _cancel_idle_timer_locked(self) -> None:
        """Останавливает фоновую задачу отслеживания бездействия."""
        task = self._idle_task
        self._idle_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _idle_watchdog(self) -> None:
        """Периодически проверяет бездействие и отключает бота при превышении idle_timeout."""
        interval = min(_IDLE_CHECK_INTERVAL, self._idle_timeout)
        while True:
            await asyncio.sleep(interval)
            should_disconnect = False
            async with self._lock:
                if self._voice_client is None:
                    return
                if self._state != PlayerState.PLAYING or self._channel_is_empty_locked():
                    now = time.monotonic()
                    if self._idle_since is None:
                        self._idle_since = now
                    elif now - self._idle_since >= self._idle_timeout:
                        should_disconnect = True
                else:
                    self._idle_since = None
            if should_disconnect:
                logger.info("Отключение по бездействию (idle_timeout=%s с)", self._idle_timeout)
                await self.disconnect()
                return
