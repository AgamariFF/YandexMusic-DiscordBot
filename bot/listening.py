"""Голосовое прослушивание (Discord-слой) поверх `bot.speech.SpeechRecognizer`.

`GuildListener` — Discord-обвязка вокруг `SpeechRecognizer` (см. её
докстринг): держит собственное голосовое соединение гильдии
(`discord.ext.voice_recv.VoiceRecvClient`, а не обычный `discord.VoiceClient`
— обычный умеет только отправлять звук, здесь же нужен приём, как и у
`bot.roulette.GuildRoulette`) и мост звука в одну сторону — из Discord в
распознавание. В обратную сторону (бот что-то говорит) прослушиванию
отправлять нечего, поэтому, в отличие от `GuildRoulette`, здесь нет ни
исходящего аудиоисточника, ни `voice_client.play(...)`.

Взаимоисключение с `bot.player.GuildPlayer` и `bot.roulette.GuildRoulette`
(все три делят одно голосовое соединение гильдии — Discord не даёт открыть
второе, пока активно первое) реализовано только в одну сторону — забота
вызывающего кода: запуск прослушивания сначала принудительно останавливает
и волну, и чат-рулетку (см. докстринг `bot.cogs.listen.ListenCog`).
Обратной защиты нет: попытка запустить волну или рулетку поверх уже
идущего прослушивания просто упадёт с `VoiceConnectError`, потому что
Discord не даёт открыть второе голосовое соединение гильдии, — то же самое
временное ограничение и по той же причине, что уже описано в докстринге
`bot.roulette`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

import av
import discord
from discord.ext import voice_recv

from bot.errors import VoiceConnectError
from bot.nekto.audio import DISCORD_CHANNELS, DISCORD_SAMPLE_RATE, DISCORD_SAMPLE_WIDTH
from bot.speech import SAMPLE_RATE, SpeechRecognizer
from bot.voice_dave import install_dave_decryption

logger = logging.getLogger(__name__)

# Глубина очереди кадров одного говорящего в кадрах по 20 мс — тот же запас
# (~1 секунда), что и у очередей rulette/nekto (см. bot.nekto.audio), по
# тому же обоснованию: пережить короткую просадку в обработке, не растя
# при этом задержку между произнесённым словом и распознанным текстом.
_MAX_QUEUED_FRAMES = 50


def _build_speech_resampler() -> av.AudioResampler:
    """Собирает ресемплер, приводящий кадры Discord (48 кГц/стерео) к формату Vosk (16 кГц/моно).

    Как и `build_discord_resampler` в bot.nekto.audio, но в обратную
    сторону: там из чужого формата в формат Discord, здесь наоборот — из
    формата Discord в формат, который понимает `bot.speech.SpeechRecognizer`.
    """
    return av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)


class _DiscordToSpeechSink(voice_recv.AudioSink):
    """Передаёт PCM говорящих из голосового канала в распознавание: Discord → `SpeechRecognizer`.

    Устроен по образцу `bot.roulette._DiscordToPeerSink` (см. её докстринг
    про то же самое подробнее): `discord-ext-voice-recv` вызывает `write()`
    из собственного потока чтения голосового UDP-сокета — не из потока
    event loop, — синхронно и отдельно для каждого говорящего. Передача
    кадра дальше идёт через `loop.call_soon_threadsafe`, а не прямым
    вызовом отсюда. В отличие от рулетки, кадры разных говорящих здесь не
    нужно ничем смешивать — у каждого свой независимый путь обработки (см.
    `GuildListener._pump_speaker`), поэтому кадр просто передаётся дальше
    вместе с идентификатором говорящего.
    """

    def __init__(self, listener: GuildListener) -> None:
        """Запоминает владеющий `GuildListener` — через него достаёт event loop и очереди."""
        super().__init__()
        self._listener = listener
        self._decoders: dict[int, discord.opus.Decoder] = {}
        self._last_opus_error_log = 0.0
        self._last_unknown_user_log = 0.0

    def wants_opus(self) -> bool:
        """Получает Opus и декодирует его с защитой от битых UDP-пакетов."""
        return True

    def write(self, user: discord.Member | discord.User | None, data: voice_recv.VoiceData) -> None:
        """Декодирует Opus говорящего и передаёт PCM в event loop вместе с его идентификатором."""
        if not data.opus:
            return
        if user is None:
            # Пакет пришёл раньше, чем сопоставление ssrc -> пользователь
            # (событие голосового шлюза) успело прийти — обычно это первые
            # несколько пакетов только что заговорившего. Без идентификатора
            # говорящего распознавать некому его приписать — пропускаем.
            now = time.monotonic()
            if now - self._last_unknown_user_log >= 5.0:
                logger.debug(
                    "Пропускается пакет без известного говорящего (ssrc=%s)", data.packet.ssrc
                )
                self._last_unknown_user_log = now
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
            # См. bot.roulette._DiscordToPeerSink.write — тот же осознанный
            # выбор: отбросить хвост, а не ронять приём из-за одного пакета.
            pcm = pcm[: samples * sample_size]
        frame = av.AudioFrame(format="s16", layout="stereo", samples=samples)
        frame.sample_rate = DISCORD_SAMPLE_RATE
        memoryview(frame.planes[0])[:] = pcm
        self._listener._forward_from_discord_threadsafe(user.id, frame)

    def cleanup(self) -> None:
        """Явно нечего освобождать — декодеры лишь копятся в словаре на время жизни сина."""
        self._decoders.clear()


class GuildListener:
    """Управляет голосовым прослушиванием одного сервера: соединение и мост звука в распознавание.

    Один экземпляр на сервер, по образцу `GuildPlayer`/`GuildRoulette`: свой
    лок на операции с соединением и по одной фоновой задаче на каждого
    говорящего (см. `_pump_speaker`), а не одна общая — у каждого своя
    очередь кадров и свой ресемплер (об этом подробнее в докстринге
    `_pump_speaker`).
    """

    def __init__(
        self,
        *,
        recognizer: SpeechRecognizer,
        on_phrase: Callable[[int, str], Awaitable[None]] | None = None,
    ) -> None:
        """Запоминает распознаватель речи и колбэк на распознанную фразу.

        Само соединение открывает `start()` — конструктор сети не касается.
        """
        self._recognizer = recognizer
        self._on_phrase = on_phrase

        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._voice_client: voice_recv.VoiceRecvClient | None = None
        self._channel: discord.VoiceChannel | discord.StageChannel | None = None

        self._queues: dict[int, asyncio.Queue[av.AudioFrame]] = {}
        self._pump_tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def is_active(self) -> bool:
        """Признак того, что прослушивание сейчас запущено (есть голосовое соединение)."""
        return self._voice_client is not None

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        """Голосовой канал, к которому подключено прослушивание, либо None."""
        return self._channel

    async def start(self, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        """Загружает модель речи (если ещё не загружена), подключается к каналу и начинает слушать.

        Идемпотентен по подключению: повторный вызов для того же канала при
        уже запущенном прослушивании — no-op, для другого канала —
        переподключение (`move_to`). `ensure_ready()` вызывается ДО
        подключения к голосу намеренно: если модели нет на диске, лучше
        сразу понятная ошибка (`SpeechModelUnavailableError`, см.
        `bot.speech`), чем открытое голосовое соединение без единого шанса
        хоть что-то распознать.
        """
        await self._recognizer.ensure_ready()
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._voice_client is not None and self._voice_client.is_connected():
                current_channel = self._voice_client.channel
                if current_channel is None or current_channel.id != channel.id:
                    await self._voice_client.move_to(channel)
                self._channel = channel
                return
            self._voice_client = await self._connect_voice_locked(channel)
            self._voice_client.listen(_DiscordToSpeechSink(self))
            # Discord требует DAVE (сквозное шифрование) на этом канале —
            # тот же приём, что и у чат-рулетки, подробности см. докстринг
            # bot.voice_dave.
            install_dave_decryption(self._voice_client)
            self._channel = channel

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

    async def stop(self) -> None:
        """Останавливает прослушивание, отключается от канала. Идемпотентен.

        Не бросает исключений, даже если прослушивание уже не было
        запущено — команда /listen_stop должна молча срабатывать в любом
        состоянии, а не требовать от пользователя знать текущее.
        """
        async with self._lock:
            await self._teardown_locked()

    async def _teardown_locked(self) -> None:
        """Останавливает фоновые задачи всех говорящих и рвёт голосовое соединение."""
        for speaker_id in list(self._pump_tasks):
            await self._drop_speaker_locked(speaker_id)

        if self._voice_client is not None:
            try:
                self._voice_client.stop()  # прекращает listen()
            except Exception:
                logger.warning("Ошибка при остановке приёма голоса прослушивания", exc_info=True)
            try:
                await self._voice_client.disconnect(force=True)
            except Exception:
                logger.warning(
                    "Ошибка при отключении голосового канала прослушивания", exc_info=True
                )
        self._voice_client = None
        self._channel = None

    def drop_speaker(self, speaker_id: int) -> None:
        """Убирает состояние говорящего, покинувшего голосовой канал — не дожидаясь таймаута.

        Не async и не блокирует вызывающий код: сама отмена фоновой задачи
        говорящего и освобождение его распознавателя планируются отдельной
        задачей на потоке event loop. Вызывать нужно из потока event loop
        (например, из обработчика `on_voice_state_update`, см.
        `bot.cogs.listen.ListenCog`) — метод не потокобезопасен, в отличие
        от `_forward_from_discord_threadsafe`.
        """
        loop = self._loop
        if loop is None:
            return
        loop.create_task(self._drop_speaker(speaker_id))

    async def _drop_speaker(self, speaker_id: int) -> None:
        """Захватывает общий лок и убирает состояние говорящего.

        Асинхронная обёртка над `_drop_speaker_locked` для вызова из
        задачи, запланированной синхронным `drop_speaker`.
        """
        async with self._lock:
            await self._drop_speaker_locked(speaker_id)

    async def _drop_speaker_locked(self, speaker_id: int) -> None:
        """Останавливает фоновую задачу говорящего и освобождает его распознаватель."""
        self._queues.pop(speaker_id, None)
        task = self._pump_tasks.pop(speaker_id, None)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._recognizer.drop_speaker(speaker_id)

    def _forward_from_discord_threadsafe(self, speaker_id: int, frame: av.AudioFrame) -> None:
        """Планирует постановку кадра говорящего в очередь на потоке event loop.

        Вызывается из `_DiscordToSpeechSink.write()` — то есть из чужого
        потока (см. её докстринг) — поэтому единственная безопасная отсюда
        операция — запланировать синхронный колбэк через `call_soon_threadsafe`.
        """
        loop = self._loop
        if loop is None:
            return
        loop.call_soon_threadsafe(self._enqueue_frame, speaker_id, frame)

    def _enqueue_frame(self, speaker_id: int, frame: av.AudioFrame) -> None:
        """На потоке event loop: кладёт кадр в очередь говорящего, заводя её при первом кадре.

        Очередь и фоновая задача на каждого говорящего заводятся лениво —
        постоянный пул под всех потенциальных участников сервера заранее не
        нужен, заводим только для тех, кто реально заговорил.
        """
        queue = self._queues.get(speaker_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=_MAX_QUEUED_FRAMES)
            self._queues[speaker_id] = queue
            assert self._loop is not None
            self._pump_tasks[speaker_id] = self._loop.create_task(
                self._pump_speaker(speaker_id, queue)
            )
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                queue.get_nowait()
            queue.put_nowait(frame)

    async def _pump_speaker(self, speaker_id: int, queue: asyncio.Queue[av.AudioFrame]) -> None:
        """Ресемплит и распознаёт речь одного говорящего, пока задача не будет отменена.

        Отдельная задача и отдельный ресемплер на каждого говорящего —
        `av.AudioResampler` хранит внутреннее состояние фильтра, смешивать в
        нём кадры разных людей нельзя (та же причина, что и у отдельного
        `KaldiRecognizer` на говорящего в `bot.speech.SpeechRecognizer`).
        Единственный читатель очереди — эта же задача, поэтому порядок
        обработки кадров одного человека сохраняется сам собой, без
        дополнительной синхронизации. Само распознавание не блокирует
        event loop целиком, даже пока эта задача им «занята»: тяжёлая часть
        (`AcceptWaveform`) внутри `SpeechRecognizer.feed()` уходит в
        отдельный поток, так что задачи других говорящих продолжают
        выполняться параллельно, пока эта ждёт результат.
        """
        resampler = _build_speech_resampler()
        while True:
            frame = await queue.get()
            for resampled in resampler.resample(frame):
                pcm = bytes(resampled.planes[0])
                if not pcm:
                    continue
                text = await self._recognizer.feed(speaker_id, pcm)
                if text:
                    await self._emit_phrase(speaker_id, text)

    async def _emit_phrase(self, speaker_id: int, text: str) -> None:
        """Отдаёт распознанную фразу колбэку; ошибки колбэка не роняют прослушивание."""
        if self._on_phrase is None:
            return
        try:
            await self._on_phrase(speaker_id, text)
        except Exception:
            logger.exception("Ошибка в колбэке распознанной фразы прослушивания")
