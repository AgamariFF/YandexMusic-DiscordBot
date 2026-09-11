"""Голосовая чат-рулетка (nekto.me) для одного голосового сервера Discord.

`GuildRoulette` — Discord-слой поверх готовой `bot.nekto.NektoSession`
(см. её докстринг): держит собственное голосовое соединение гильдии
(`discord.ext.voice_recv.VoiceRecvClient`, а не обычный `discord.VoiceClient`
— обычный умеет только отправлять звук, здесь же нужен ещё и приём) и мост
аудио в обе стороны между этим соединением и сессией.

Взаимоисключение с `bot.player.GuildPlayer` (Discord не даёт открыть второе
голосовое соединение гильдии, пока активно первое) — забота вызывающего
кода, здесь не реализовано; см. докстринг `bot.cogs.roulette.RouletteCog`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import queue as sync_queue
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

import av
import discord
from discord.ext import voice_recv

from bot.errors import BotError, RouletteNotActiveError, VoiceConnectError
from bot.nekto import (
    BannedEvent,
    NektoEvent,
    NektoSession,
    PeerFoundEvent,
    PeerLeftEvent,
    ProtocolErrorEvent,
)
from bot.nekto.audio import (
    DISCORD_CHANNELS,
    DISCORD_FRAME_SAMPLES,
    DISCORD_SAMPLE_RATE,
    DISCORD_SAMPLE_WIDTH,
    build_discord_resampler,
    frame_to_pcm,
)
from bot.voice_dave import install_dave_decryption

logger = logging.getLogger(__name__)

# Байтов в одном 20-мс PCM-кадре формата Discord (960 сэмплов * 2 канала * 2
# байта) — тот же размер, что и `discord.opus.Encoder.FRAME_SIZE`, но
# вычислен из констант bot.nekto.audio, а не из ctypes-обёртки opus, чтобы не
# тянуть загрузку библиотеки opus только ради одного числа.
_FRAME_BYTES = DISCORD_FRAME_SAMPLES * DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
_SILENCE_FRAME = b"\x00" * _FRAME_BYTES

# Глубина очереди исходящего к Discord звука в кадрах — тот же запас
# (~1 секунда при кадрах по 20 мс), что и у очередей самой nekto-сессии (см.
# bot.nekto.audio._MAX_QUEUED_FRAMES): достаточно, чтобы пережить короткую
# просадку без накопления растущей задержки разговора.
_MAX_QUEUED_CHUNKS = 50


class RouletteStatus(StrEnum):
    """Состояние чат-рулетки, отражаемое в сообщении-статусе."""

    SEARCHING = "searching"
    FOUND = "found"
    LEFT = "left"
    STOPPED = "stopped"


class _PeerAudioSource(discord.AudioSource):
    """Отдаёт discord.py PCM-кадры собеседника, читая их из потокобезопасной очереди.

    `read()` discord.py вызывает синхронно из отдельного потока плеера ровно
    каждые 20 мс (см. предупреждение в docstring `discord.AudioSource`) — он
    не может ждать сеть или лок event loop. Наполняет очередь `_pump_peer_audio`
    (корутина в event loop), а `read()` только читает уже готовые кадры;
    пустая очередь — не ошибка (собеседник молчит или ещё не найден), тогда
    отдаётся тишина нужного размера. Пустые bytes отдавать нельзя: discord.py
    трактует их как конец потока и останавливает воспроизведение целиком.
    """

    def __init__(self) -> None:
        """Готовит пустую потокобезопасную очередь кадров."""
        self._queue: sync_queue.Queue[bytes] = sync_queue.Queue(maxsize=_MAX_QUEUED_CHUNKS)

    def push_chunk(self, chunk: bytes) -> None:
        """Кладёт готовый 20-мс PCM-кадр в очередь; при переполнении отбрасывает старый."""
        try:
            self._queue.put_nowait(chunk)
        except sync_queue.Full:
            with contextlib.suppress(sync_queue.Empty):
                self._queue.get_nowait()
            self._queue.put_nowait(chunk)

    def read(self) -> bytes:
        """Синхронно отдаёт готовый кадр либо тишину нужного размера, если очередь пуста."""
        try:
            return self._queue.get_nowait()
        except sync_queue.Empty:
            return _SILENCE_FRAME

    def is_opus(self) -> bool:
        """PCM, не Opus — кодирует сам discord.py, как и остальные источники бота."""
        return False

    def cleanup(self) -> None:
        """Явно нечего освобождать: очередь просто перестаёт наполняться после остановки."""


class _DiscordToPeerSink(voice_recv.AudioSink):
    """Пересылает PCM говорящих из голосового канала собеседнику: Discord → `NektoSession`.

    `discord-ext-voice-recv` вызывает `write()` из собственного потока чтения
    голосового UDP-сокета — не из потока event loop — синхронно и отдельно
    для каждого говорящего пользователя. `NektoSession.send_audio` изнутри
    трогает `asyncio.Queue` (см. `bot.nekto.audio.OutgoingAudioTrack.push_frame`),
    а операции над `asyncio.Queue`/`asyncio.Future` не потокобезопасны вне
    потока event loop — поэтому передача кадра идёт через
    `loop.call_soon_threadsafe`, а не прямым вызовом из этого потока.

    Несколько говорящих одновременно: библиотека шлёт отдельный кадр на
    каждого, а собеседнику можно передать только один аудиопоток. Решение —
    пересылать кадр каждого говорящего как есть, без микширования: честное
    микширование PCM без точной синхронизации по времени прихода пакетов
    разных пользователей исказило бы звук сильнее, чем редкая одновременная
    речь двух человек в голосовом канале Discord (для этой фичи обычно
    небольшом). В типичном случае, когда говорит один человек, разницы с
    микшированием нет вовсе.
    """

    def __init__(self, roulette: GuildRoulette) -> None:
        """Запоминает владеющий `GuildRoulette` — через него достаёт сессию и event loop."""
        super().__init__()
        self._roulette = roulette
        self._decoders: dict[int, discord.opus.Decoder] = {}
        self._last_opus_error_log = 0.0

    def wants_opus(self) -> bool:
        """Получает Opus и декодирует его с защитой от битых UDP-пакетов."""
        return True

    def write(self, user: discord.Member | discord.User | None, data: voice_recv.VoiceData) -> None:
        """Декодирует Opus говорящего и передаёт PCM в event loop."""
        if not data.opus:
            return
        decoder = self._decoders.setdefault(data.packet.ssrc, discord.opus.Decoder())
        try:
            pcm = decoder.decode(data.opus, fec=False)
        except discord.opus.OpusError:
            now = time.monotonic()
            if now - self._last_opus_error_log >= 5.0:
                logger.debug("Пропускаются повреждённые Opus-пакеты от %s", user)
                self._last_opus_error_log = now
            return
        sample_size = DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
        samples, remainder = divmod(len(pcm), sample_size)
        if samples == 0:
            return
        if remainder:
            # Пакет с нецелым числом стерео-сэмплов штатно не должен
            # возникать при s16-декодировании — на всякий случай отбрасываем
            # хвост, а не роняем мост из-за одного кривого пакета.
            pcm = pcm[: samples * sample_size]
        frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
        frame.sample_rate = DISCORD_SAMPLE_RATE
        memoryview(frame.planes[0])[:] = pcm
        self._roulette._forward_from_discord_threadsafe(frame)

    def cleanup(self) -> None:
        """Явно нечего освобождать — сессия и event loop живут вне сина, за пределами его жизни."""
        self._decoders.clear()


class GuildRoulette:
    """Управляет голосовой чат-рулеткой одного сервера: соединение, сессия nekto.me и аудио-мост.

    Один экземпляр на сервер, аналогично `bot.player.GuildPlayer`: свой лок
    на все операции, свой голосовой клиент и свои фоновые задачи (обработка
    событий сессии и перекачка входящего звука).
    """

    def __init__(
        self,
        *,
        token: str,
        user_agent: str,
        on_status_changed: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Запоминает токен/user-agent сервиса и колбэк обновления статуса.

        Само соединение (голосовое и с nekto.me) открывает `start()` —
        конструктор сети не касается.
        """
        self._token = token
        self._user_agent = user_agent
        self._on_status_changed = on_status_changed

        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._voice_client: voice_recv.VoiceRecvClient | None = None
        self._channel: discord.VoiceChannel | discord.StageChannel | None = None
        self._source: _PeerAudioSource | None = None
        self._session: NektoSession | None = None

        self._events_task: asyncio.Task[None] | None = None
        self._pump_task: asyncio.Task[None] | None = None

        self._status = RouletteStatus.STOPPED
        self._stop_reason: str | None = None

    @property
    def status(self) -> RouletteStatus:
        """Текущее состояние чат-рулетки для сообщения-статуса."""
        return self._status

    @property
    def stop_reason(self) -> str | None:
        """Причина последней остановки (например, бан аккаунта), либо None."""
        return self._stop_reason

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        """Голосовой канал, к которому подключена рулетка, либо None."""
        return self._channel

    @property
    def is_active(self) -> bool:
        """Признак того, что рулетка сейчас запущена (сессия с nekto.me открыта)."""
        return self._session is not None

    async def start(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Подключается к каналу (если ещё не там) и начинает поиск собеседника.

        Идемпотентен по подключению: повторный вызов для того же канала при
        уже запущенной рулетке просто перезапускает поиск (полезно как
        обработчик команды /roulette без разбора, "первый это запуск или
        нет"). Взаимоисключение с `GuildPlayer` на этом же сервере здесь не
        реализовано — обязан обеспечить вызывающий код (см. docstring
        `bot.cogs.roulette.RouletteCog`); без явной остановки волны попытка
        подключения ниже просто упадёт с `VoiceConnectError`, потому что
        Discord не даёт открыть второе голосовое соединение гильдии.
        """
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._voice_client is None or not self._voice_client.is_connected():
                self._voice_client = await self._connect_voice_locked(channel)
                self._source = _PeerAudioSource()
                self._voice_client.play(self._source)
                self._voice_client.listen(_DiscordToPeerSink(self))
                # Discord требует DAVE (сквозное шифрование) на этом канале —
                # без этого слоя приём голоса падает с CryptoError либо
                # декодирует ещё DAVE-зашифрованный Opus как сырой. Подробности
                # и почему подмена именно здесь и именно так — см. докстринг
                # bot.voice_dave.
                install_dave_decryption(self._voice_client)
            elif self._voice_client.channel is None or self._voice_client.channel.id != channel.id:
                await self._voice_client.move_to(channel)
            self._channel = channel

            if self._session is None:
                session = NektoSession(self._token, user_agent=self._user_agent)
                try:
                    await session.connect()
                except BotError:
                    await self._teardown_locked()
                    raise
                self._session = session
                self._events_task = self._loop.create_task(self._consume_events())
                self._pump_task = self._loop.create_task(self._pump_peer_audio())

            await self._session.start_search()
            await self._set_status_locked(RouletteStatus.SEARCHING)

    async def _connect_voice_locked(
        self, channel: discord.VoiceChannel | discord.StageChannel
    ) -> voice_recv.VoiceRecvClient:
        """Открывает голосовое соединение с приёмом звука, переводя ошибки в `VoiceConnectError`."""
        try:
            return await channel.connect(cls=voice_recv.VoiceRecvClient)
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

    async def next_peer(self) -> None:
        """Завершает разговор с текущим собеседником (если есть) и ищет нового."""
        async with self._lock:
            if self._session is None:
                raise RouletteNotActiveError()
            await self._session.next_peer()
            await self._set_status_locked(RouletteStatus.SEARCHING)

    async def stop(self, *, reason: str | None = None) -> None:
        """Останавливает рулетку, закрывает сессию и отключается от канала. Идемпотентен.

        Не бросает исключений, даже если рулетка уже не была запущена —
        команда /roulette_stop и кнопка «Стоп» должны молча срабатывать в
        любом состоянии, а не требовать от пользователя знать текущее.
        """
        async with self._lock:
            was_active = self._voice_client is not None or self._session is not None
            await self._teardown_locked()
            if was_active:
                await self._set_status_locked(RouletteStatus.STOPPED, reason=reason)

    async def _teardown_locked(self) -> None:
        """Останавливает аудио-мост и сессию, отключается от канала. Общая часть `start`/`stop`.

        Порядок важен: сначала фоновая перекачка звука (иначе в уже
        наполовину остановленный мост ещё может прийти кадр), затем сессия
        nekto.me (её `close()` заодно и будит `_consume_events` — см.
        докстринг `NektoSession.events`), и только потом рвётся голосовое
        соединение Discord.
        """
        await self._cancel_task_locked("_pump_task")

        if self._session is not None:
            session = self._session
            self._session = None
            await session.close()

        await self._cancel_task_locked("_events_task")

        if self._voice_client is not None:
            try:
                self._voice_client.stop()  # прекращает и play(), и listen()
            except Exception:
                logger.warning("Ошибка при остановке аудио-моста чат-рулетки", exc_info=True)
            try:
                await self._voice_client.disconnect(force=True)
            except Exception:
                logger.warning("Ошибка при отключении голосового канала чат-рулетки", exc_info=True)
        self._voice_client = None
        self._channel = None
        self._source = None

    async def _cancel_task_locked(self, attr: str) -> None:
        """Отменяет и дожидается фоновую задачу по имени атрибута, если это не текущая задача.

        Проверка "это не текущая задача" нужна для случая, когда `stop()`
        вызывается изнутри самой `_consume_events` (обработка `BannedEvent`,
        см. `_handle_event`): попытка отменить и дождаться саму себя —
        гарантированный дедлок/преждевременная отмена середины teardown.
        Задача в этом случае просто доработает и завершится сама сразу после
        возврата из текущего вызова — как и `GuildPlayer._cancel_idle_timer_locked`.
        """
        task: asyncio.Task[None] | None = getattr(self, attr)
        setattr(self, attr, None)
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _set_status_locked(
        self, status: RouletteStatus, *, reason: str | None = None
    ) -> None:
        """Обновляет статус и вызывает колбэк. Предполагает, что `self._lock` уже захвачен."""
        self._status = status
        self._stop_reason = reason
        if self._on_status_changed is not None:
            try:
                await self._on_status_changed()
            except Exception:
                logger.exception("Ошибка в колбэке обновления статуса чат-рулетки")

    async def _set_status(self, status: RouletteStatus, *, reason: str | None = None) -> None:
        """Обновляет статус, сама захватывая лок — для вызовов вне уже заблокированного контекста.

        Используется только из `_handle_event` (фоновая задача, лока не
        держит); `start`/`next_peer`/`stop` уже держат лок сами и вызывают
        `_set_status_locked` напрямую, без повторного захвата.
        """
        async with self._lock:
            await self._set_status_locked(status, reason=reason)

    async def _consume_events(self) -> None:
        """Обрабатывает события активной сессии nekto.me, пока она не закрыта."""
        session = self._session
        if session is None:
            return
        async for event in session.events():
            await self._handle_event(event)

    async def _handle_event(self, event: NektoEvent) -> None:
        """Разбирает одно событие сессии и отражает его в статусе чат-рулетки."""
        if isinstance(event, PeerFoundEvent):
            await self._set_status(RouletteStatus.FOUND)
        elif isinstance(event, PeerLeftEvent):
            await self._set_status(RouletteStatus.LEFT)
            await self._restart_search_if_active()
        elif isinstance(event, BannedEvent):
            logger.error("Аккаунт чат-рулетки заблокирован сервисом nekto.me: %r", event.ban_info)
            await self.stop(reason="Аккаунт заблокирован сервисом чат-рулетки.")
        elif isinstance(event, ProtocolErrorEvent):
            logger.warning("Ошибка протокола чат-рулетки: %s", event.description)
            await self._restart_search_if_active()

    async def _restart_search_if_active(self) -> None:
        """Возобновляет поиск после ухода собеседника/ошибки протокола, если рулетка ещё активна.

        "Активна" значит: рулетку ещё не остановили (`self._session` не
        None) и сессия сейчас ничем не занята (`state == "connected"`) — то
        есть событие действительно означает "нужно снова искать", а не гонку
        с уже идущим поиском/разговором.
        """
        session = self._session
        if session is None or session.state != "connected":
            return
        try:
            await session.start_search()
        except BotError:
            logger.exception("Не удалось возобновить поиск собеседника чат-рулетки")
            return
        await self._set_status(RouletteStatus.SEARCHING)

    async def _pump_peer_audio(self) -> None:
        """Перекачивает звук собеседника в discord-источник, приводя формат через ресемплер.

        Собеседник может присылать не тот формат, что нужен Discord (моно,
        другая частота дискретизации) — `build_discord_resampler()` приводит
        любой входной формат к PCM 48 кГц/стерео/16 бит. Ресемплер сам решает,
        сколько сэмплов отдать на каждый вызов `resample()` (может быть
        больше или меньше одного 20-мс кадра), поэтому результат копится в
        `buffer` и режется на ровные 20-мс кадры перед тем, как уйти в
        очередь источника — `_PeerAudioSource.read()` ожидает кадры строго
        фиксированного размера.
        """
        session = self._session
        source = self._source
        if session is None or source is None:
            return
        resampler = build_discord_resampler()
        buffer = bytearray()
        while True:
            frame = await session.receive_audio()
            for resampled in resampler.resample(frame):
                buffer.extend(frame_to_pcm(resampled))
            while len(buffer) >= _FRAME_BYTES:
                source.push_chunk(bytes(buffer[:_FRAME_BYTES]))
                del buffer[:_FRAME_BYTES]

    def _forward_from_discord_threadsafe(self, frame: av.AudioFrame) -> None:
        """Планирует передачу кадра из Discord в активную сессию на потоке event loop.

        Вызывается из `_DiscordToPeerSink.write()` — то есть из чужого
        потока (см. её докстринг) — поэтому сама передача кадра идёт через
        `call_soon_threadsafe`, а не прямым вызовом отсюда.
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._send_audio_if_active, frame)

    def _send_audio_if_active(self, frame: av.AudioFrame) -> None:
        """Отдаёт кадр активной сессии, если она сейчас есть; иначе молча отбрасывает.

        Выполняется уже на потоке event loop (см. `_forward_from_discord_threadsafe`),
        поэтому здесь можно безопасно трогать `self._session`.
        """
        session = self._session
        if session is not None:
            session.send_audio(frame)
