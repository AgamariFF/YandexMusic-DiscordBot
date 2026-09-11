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
from discord.ext import voice_recv

from bot.audio import BassLevel, TrackedAudioSource, create_source
from bot.audio.speaking import SpeakingSource
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

# Страховка на случай, если фраза почему-то не договорится (соединение
# оборвалось прямо во время неё, источник заменили). Без предела ожидающий
# завис бы навсегда; сама длина фразы ограничена куда строже в `bot.tts`.
_MAX_SPEECH_SECONDS = 60.0


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
        announce: Callable[[TrackInfo, str | None], Awaitable[None]] | None = None,
        on_stopped: Callable[[], Awaitable[None]] | None = None,
        on_voice_connected: Callable[[voice_recv.VoiceRecvClient], Awaitable[None]] | None = None,
        on_voice_disconnected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Создаёт плеер сервера с заданными настройками звука и оповещений.

        `on_stopped` — необязательный колбэк без аргументов, симметричный
        `announce`: срабатывает ровно один раз при каждом реальном переходе
        волны из активного состояния в остановленное — независимо от
        причины (кончились треки, `disconnect()` и т.д.). Подробности и
        гарантия однократности — в докстринге `_stop_locked_state`.

        `on_voice_connected`/`on_voice_disconnected` — необязательная пара
        колбэков вокруг жизни голосового соединения, а не воспроизведения:
        первый срабатывает в `connect()` сразу после успешного подключения
        (в том числе и после `move_to` в другой канал), второй — в
        `disconnect()` непосредственно перед разрывом уже установленного
        соединения. Они существуют для `bot.cogs.voice_control.VoiceControlCog`
        — соединение открывает и закрывает плеер, а распознавание речи живёт
        на том же самом соединении (`discord.ext.voice_recv.VoiceRecvClient`
        умеет и `play()`, и `listen()` одновременно), поэтому ему нужно
        знать о появлении и исчезновении соединения, не открывая своего.
        Как и `announce`/`on_stopped`, исключение колбэка не должно ронять
        подключение/отключение плеера — оно логируется и проглатывается.
        """
        self._client = client
        self._ffmpeg_path = ffmpeg_path
        self._volume = max(0.0, min(2.0, default_volume))
        self._bass = BassLevel.OFF
        self._idle_timeout = idle_timeout
        self._announce = announce
        self._on_stopped = on_stopped
        self._on_voice_connected = on_voice_connected
        self._on_voice_disconnected = on_voice_disconnected

        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._voice_client: voice_recv.VoiceRecvClient | None = None
        self._channel: discord.VoiceChannel | discord.StageChannel | None = None
        self._session: WaveSession | None = None
        self._current_track: TrackInfo | None = None
        self._source: SpeakingSource | None = None
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
    def voice_client(self) -> voice_recv.VoiceRecvClient | None:
        """Активное голосовое соединение сервера либо None.

        Тип — `VoiceRecvClient` (подкласс обычного `discord.VoiceClient` с
        добавленным приёмом звука), а не просто `VoiceClient`: соединение
        должно уметь и `play()`, и `listen()` одновременно, потому что на
        нём же может жить распознавание речи (см. докстринг `__init__` про
        `on_voice_connected`). Для остального кода это прозрачно — весь
        обычный API `VoiceClient` (`play`, `pause`, `is_playing` и т.д.)
        доступен как раньше.
        """
        return self._voice_client

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        """Голосовой канал, к которому подключён плеер, либо None."""
        return self._channel

    @property
    def wave_description(self) -> str | None:
        """Человекочитаемое описание текущей волны, либо None, если волна не запущена."""
        return self._session.description if self._session is not None else None

    async def connect(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Подключается к каналу либо переходит в него, если уже подключён к другому.

        Подключается через `cls=voice_recv.VoiceRecvClient` — не потому,
        что плееру самому нужен приём звука, а потому, что распознавание
        речи (`bot.cogs.voice_control.VoiceControlCog`) делит с плеером
        одно и то же голосовое соединение гильдии (Discord не даёт открыть
        второе) и должно иметь возможность слушать на нём же. `on_voice_connected`
        вызывается уже вне `self._lock`, чтобы колбэк (может включать
        загрузку модели распознавания при первом подключении) не держал
        лок плеера и не блокировал остальные его операции.
        """
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            try:
                if self._voice_client is not None and self._voice_client.is_connected():
                    await self._voice_client.move_to(channel)
                else:
                    self._voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            # discord.py сигнализирует отсутствие PyNaCl (без него голос не работает)
            # голым RuntimeError, а не своим типом исключения — перехватываем и его.
            except (
                TimeoutError,
                discord.ClientException,
                discord.opus.OpusNotLoaded,
                RuntimeError,
            ) as exc:
                raise VoiceConnectError(
                    f"Не удалось подключиться к голосовому каналу {channel.id}: {exc}",
                    user_message="Не удалось подключиться к голосовому каналу.",
                ) from exc
            self._channel = channel
            self._restart_idle_timer_locked()

        if self._on_voice_connected is not None:
            try:
                await self._on_voice_connected(self._voice_client)
            except Exception:
                logger.exception("Ошибка в колбэке подключения голосового соединения")

    async def disconnect(self) -> None:
        """Отключается от голосового канала и сбрасывает состояние. Идемпотентен."""
        async with self._lock:
            await self._cancel_idle_timer_locked()
            self._stop_playback_locked()
            if self._session is not None:
                # Сессия ещё жива — досылаем фидбек (например, trackStarted
                # по играющему треку) до того, как `_stop_locked_state`
                # выбросит объект вместе с его очередью.
                await self._session.flush_pending_feedbacks()
            await self._stop_locked_state()
            self._consecutive_failures = 0
            if self._voice_client is not None:
                if self._on_voice_disconnected is not None:
                    # До разрыва соединения — распознаванию ещё есть с чего
                    # снимать `listen()` (см. докстринг __init__ про
                    # on_voice_connected/on_voice_disconnected).
                    try:
                        await self._on_voice_disconnected()
                    except Exception:
                        logger.exception("Ошибка в колбэке отключения голосового соединения")
                try:
                    await self._voice_client.disconnect(force=True)
                except Exception:
                    logger.warning("Ошибка при отключении от голосового канала", exc_info=True)
            self._voice_client = None
            self._channel = None

    async def start_wave(self) -> TrackInfo:
        """Запускает «Мою волну» заново и начинает воспроизведение первого трека."""
        return await self._start_wave_with_session(WaveSession(self._client))

    async def start_wave_from_track(self, track: TrackInfo) -> TrackInfo:
        """Запускает волну от конкретного трека: сначала сам трек, затем похожие на него.

        Станция волны — `track:<id трека>` (без id альбома, здесь именно
        `TrackInfo.id`), а `track_to_start_from=track.id` — то самое поле,
        из-за которого сессия отдаёт запрошенный трек ПЕРВЫМ (см. docstring
        `YandexMusicClient.start_session`). Поэтому отдельного
        воспроизведения трека вне волны не нужно: волна с этими параметрами
        и есть «сам трек, потом волна от него». Описание волны для UI — без
        длительности из `track.display`, только артисты и название.
        """
        session = WaveSession(
            self._client,
            seeds=[f"track:{track.id}"],
            track_to_start_from=track.id,
            description=f"Моя волна по {track.artists} — {track.title}",
        )
        return await self._start_wave_with_session(session)

    async def _start_wave_with_session(self, session: WaveSession) -> TrackInfo:
        """Общая часть запуска волны: прерывание текущего трека, старт переданной сессии.

        Вынесена из `start_wave`/`start_wave_from_track`, чтобы не дублировать
        тонкую логику: прерывание играющего трека, досылку фидбека ещё живой
        СТАРОЙ сессии и сброс счётчика ошибок. Принимает уже сконструированную
        (но ещё не запущенную) `WaveSession` — обе публичные обёртки лишь по-
        разному её конструируют.
        """
        async with self._lock:
            if self._voice_client is None or not self._voice_client.is_connected():
                raise NotConnectedError()

            if self._current_track is not None:
                await self._interrupt_current_track_locked()

            # Сбрасываем фидбек СТАРОЙ сессии, пока она ещё жива (её
            # radioSessionId ещё действителен): как только `self._session`
            # ниже будет заменён новым объектом, добраться до очереди
            # старой сессии станет негде. Порядок принципиален: это должно
            # произойти ПОСЛЕ `_interrupt_current_track_locked` (иначе skip
            # ещё не попал в очередь) и ДО создания нового `WaveSession`.
            if self._session is not None:
                await self._session.flush_pending_feedbacks()

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

    async def say(self, pcm: bytes) -> None:
        """Произносит готовый PCM голосом бота, приостановив музыку на время фразы.

        На вход — уже синтезированная речь в формате Discord (см.
        `bot.tts.TextToSpeech.synthesize`): плеер намеренно ничего не знает
        про синтез и модели, его дело — вклинить готовый звук в
        воспроизведение и вернуть музыку обратно.

        Пока звучит фраза, из музыкального источника не читается ни одного
        кадра, поэтому трек не «проматывается» под голос, а именно стоит и
        продолжается ровно с прерванного места (см. `bot.audio.speaking`).
        Если музыка была на паузе, она снимается с паузы на время фразы и
        возвращается на паузу после — иначе фразу не было бы слышно вовсе,
        ведь приостановленное соединение не читает источник.

        Ожидание окончания фразы идёт ВНЕ `self._lock`: фраза длится
        секунды, и держать всё это время лок плеера значило бы подвесить и
        переключение треков, и остановку.
        """
        if not pcm:
            return
        async with self._lock:
            if self._voice_client is None:
                raise NotConnectedError()
            was_paused = self._voice_client.is_paused()
            source = self._source
            if source is None:
                # Ничего не играет — говорим отдельным источником, который
                # закончится вместе с фразой.
                source = SpeakingSource(None, loop=asyncio.get_running_loop())
                finished = source.speak(pcm)
                self._voice_client.play(source)
            else:
                finished = source.speak(pcm)
                if was_paused:
                    self._voice_client.resume()

        try:
            await asyncio.wait_for(finished.wait(), timeout=_MAX_SPEECH_SECONDS)
        except TimeoutError:
            logger.warning("Фраза не договорена за %.0f с, продолжаем", _MAX_SPEECH_SECONDS)

        async with self._lock:
            if was_paused and self._voice_client is not None and self._voice_client.is_playing():
                self._voice_client.pause()

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
                await self._stop_locked_state()
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

    async def _handle_playback_finished(self, source: SpeakingSource) -> None:
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
            await self._stop_locked_state()
            return None

        while True:
            try:
                track = await self._session.next_track()
            except (YandexAuthError, WaveUnavailableError):
                logger.exception("«Моя волна» недоступна, воспроизведение остановлено")
                await self._stop_locked_state()
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
                    await self._stop_locked_state()
                    return None
                continue
            except BotError:
                logger.exception(
                    "Не удалось запустить трек %s, воспроизведение остановлено", track.id
                )
                await self._stop_locked_state()
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
            # Музыкальный источник всегда обёрнут говорящим (см.
            # `bot.audio.speaking`): так фраза бота может вклиниться в уже
            # идущий трек, приостановив его, — второе воспроизведение
            # поверх первого Discord не допускает.
            source = SpeakingSource(source, loop=asyncio.get_running_loop())
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
                await self._announce(track, self.wave_description)
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

        Сброс `self._source` до вызова `stop_playing()` гарантирует, что
        колбэк, который придёт по уже остановленному источнику, распознает
        себя как чужой (identity-проверка в `_handle_playback_finished`) и
        не продвинет очередь повторно.

        Вызывается именно `stop_playing()`, а не `stop()`: на `VoiceRecvClient`
        (см. `connect()`) `stop()` останавливает разом и воспроизведение, и
        приём звука (`stop()` = `stop_playing()` + `stop_listening()`), а
        приём — не забота плеера и не должен обрываться сменой трека.
        """
        if self._voice_client is None:
            return
        self._source = None
        if self._voice_client.is_playing() or self._voice_client.is_paused():
            self._voice_client.stop_playing()

    async def _stop_locked_state(self) -> None:
        """Сбрасывает состояние воспроизведения и волну, не трогая голосовое соединение.

        Этот метод выбрасывает `self._session` вместе с её очередью
        неотправленного фидбека, поэтому вызывающий код, у которого сессия
        ещё исправна, обязан сам вызвать `flush_pending_feedbacks()` до
        этого вызова (так делает `disconnect()`). Четыре вызова на путях
        обработки ошибок воспроизведения (в `_advance_locked` и `set_bass`)
        сознательно этого не делают, но по разным причинам. В ветке
        `except (YandexAuthError, WaveUnavailableError)` вокруг
        `next_track()` — да, там сеть или токен уже не работают, и досылка
        фидбека настолько же обречена, насколько и получение треков.
        Остальные три (лимит `MAX_CONSECUTIVE_TRACK_FAILURES`,
        `except BotError` вокруг `_play_track_locked` в `_advance_locked`,
        путь в `set_bass`) срабатывают и при полностью живом API — это сбои
        голосового соединения Discord, ffmpeg или отдельного трека, — но
        досылать там просто нечего: очередь уже слил предыдущий
        `next_track()` через `_refresh_chain`, а `trackStarted` текущего
        трека кладётся в очередь только после успешного старта
        воспроизведения (см. `_play_track_locked`). Пропуск здесь — не
        забывчивость.

        После сброса вызывает `on_stopped`, если волна действительно была
        активна (`_session` или `_current_track` были не пустыми ДО сброса).
        Эта проверка даёт сразу два свойства колбэка: он не сработает на
        уже остановленном плеере (все шесть вызывающих мест сбрасывают эти
        поля именно здесь и только здесь, так что повторный вызов на пустом
        состоянии ничего не найдёт активным) и не сработает, если волна и не
        запускалась (`disconnect()` идемпотентен и вызывается в том числе
        когда играть было нечему). Как и `announce`, колбэк защищён от
        собственных ошибок — они не должны ронять остановку плеера.
        """
        was_active = self._session is not None or self._current_track is not None
        self._current_track = None
        self._source = None
        self._session = None
        self._state = PlayerState.IDLE

        if was_active and self._on_stopped is not None:
            try:
                await self._on_stopped()
            except Exception:
                logger.exception("Ошибка в колбэке остановки волны")

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
