"""Голосовое прослушивание (Discord-слой) поверх `bot.speech.SpeechRecognizer`.

`GuildListener` — Discord-обвязка вокруг `SpeechRecognizer` (см. её
докстринг): мост звука в одну сторону — из Discord в распознавание. В
обратную сторону (бот что-то говорит) прослушиванию отправлять нечего,
поэтому, в отличие от `bot.roulette.GuildRoulette`, здесь нет ни исходящего
аудиоисточника, ни `voice_client.play(...)`.

Своего голосового соединения у прослушивания нет — Discord даёт гильдии
ровно одно голосовое соединение, и его открывает и закрывает
`bot.player.GuildPlayer`. `GuildListener` лишь подключается к уже открытому
соединению плеера (`attach`/`detach`, см. их докстринги) и делит его через
`discord.ext.voice_recv.VoiceRecvClient` — подкласс обычного `VoiceClient`,
который умеет `play()` (им пользуется плеер) и `listen()` (им пользуется
прослушивание) одновременно на одном и том же соединении. Раздельные
`stop_playing()`/`stop_listening()` этого класса гарантируют, что смена
трека не обрывает приём голоса, а остановка распознавания — воспроизведение
(подробнее — в докстринге `GuildPlayer._stop_playback_locked` и `detach`).

Чат-рулетка (`bot.roulette.GuildRoulette`) в эту схему не входит: она
по-прежнему держит своё отдельное голосовое соединение гильдии и потому
остаётся несовместимой и с плеером, и с прослушиванием — Discord не даёт
открыть второе соединение гильдии, пока активно первое (см. докстринг
`bot.cogs.voice_control.VoiceControlCog` про то, как это обрабатывается).
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

from bot.nekto.audio import DISCORD_CHANNELS, DISCORD_SAMPLE_RATE, DISCORD_SAMPLE_WIDTH
from bot.speech import SAMPLE_RATE, SpeechRecognizer
from bot.speech_debug import SpeechRecorder
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
        recorder = self._listener.recorder
        if recorder is not None:
            # Потери считаются ДО декодирования и до всех проверок ниже:
            # пакет, отброшенный дальше по коду, всё равно пришёл, и для
            # статистики связи важен сам факт его получения.
            recorder.loss_tracker.note(user.id, data.packet.sequence)
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
    """Управляет распознаванием речи сервера: приём звука на чужом соединении, мост в распознавание.

    Один экземпляр на сервер, по образцу `GuildPlayer`/`GuildRoulette`: свой
    лок на операции с приёмом и по одной фоновой задаче на каждого
    говорящего (см. `_pump_speaker`), а не одна общая — у каждого своя
    очередь кадров и свой ресемплер (об этом подробнее в докстринге
    `_pump_speaker`). В отличие от них, собственного голосового соединения
    у этого класса нет — см. модульный докстринг.
    """

    def __init__(
        self,
        *,
        recognizer: SpeechRecognizer,
        on_phrase: Callable[[int, str], Awaitable[None]] | None = None,
        recorder: SpeechRecorder | None = None,
    ) -> None:
        """Запоминает распознаватель речи, колбэк на фразу и отладочную запись.

        К уже открытому чужому соединению подключает `attach()` —
        конструктор сети не касается. `recorder` — необязательная отладочная
        запись всего услышанного (см. `bot.speech_debug`); `None` означает,
        что запись выключена, и тогда приём идёт ровно как раньше.
        """
        self._recognizer = recognizer
        self._on_phrase = on_phrase
        self._recorder = recorder

        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

        self._voice_client: voice_recv.VoiceRecvClient | None = None

        self._queues: dict[int, asyncio.Queue[av.AudioFrame]] = {}
        self._pump_tasks: dict[int, asyncio.Task[None]] = {}

    @property
    def recorder(self) -> SpeechRecorder | None:
        """Отладочная запись услышанного либо None, если она выключена."""
        return self._recorder

    @property
    def is_active(self) -> bool:
        """Признак того, что прослушивание сейчас подключено к чьему-то голосовому соединению."""
        return self._voice_client is not None

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        """Голосовой канал, к которому подключено прослушивание, либо None.

        Читается напрямую из `voice_client.channel`, а не хранится своим
        полем: соединением владеет `GuildPlayer`, и если он перейдёт в
        другой канал (`move_to`), актуальный канал должен быть виден отсюда
        без отдельной синхронизации.
        """
        if self._voice_client is None:
            return None
        return self._voice_client.channel

    async def attach(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Загружает модель речи (если ещё не загружена) и слушает переданное соединение.

        Соединение уже открыто и принадлежит вызывающему коду (см.
        модульный докстринг) — этот метод только вешает на него приём
        звука, ничего не подключая и не отключая сам. Идемпотентен: повторный
        вызов с тем же самым соединением — no-op, с другим — сначала снимает
        приём со старого (`_detach_locked`), затем вешает на новое.
        `ensure_ready()` вызывается ДО захвата лока и ДО правки соединения
        намеренно: если модели нет на диске, лучше сразу понятная ошибка
        (`SpeechModelUnavailableError`, см. `bot.speech`), чем наполовину
        подключённое прослушивание.
        """
        await self._recognizer.ensure_ready()
        self._loop = asyncio.get_running_loop()
        async with self._lock:
            if self._voice_client is voice_client:
                return
            if self._voice_client is not None:
                await self._detach_locked()
            voice_client.listen(_DiscordToSpeechSink(self))
            # Discord требует DAVE (сквозное шифрование) на этом канале —
            # тот же приём, что и у чат-рулетки, подробности см. докстринг
            # bot.voice_dave. install_dave_decryption работает с читателем,
            # который появляется только после listen() — порядок принципиален.
            install_dave_decryption(voice_client)
            self._voice_client = voice_client

    async def detach(self) -> None:
        """Останавливает приём звука, не трогая само голосовое соединение. Идемпотентен.

        Не бросает исключений ни в каком состоянии, даже если прослушивание
        уже не было подключено: вызывается из хука отключения плеера (см.
        `bot.cogs.voice_control.VoiceControlCog`) и не должна мешать
        отключению плеера от голосового канала. Останавливает именно
        `voice_client.stop_listening()`, а не `stop()` — `stop()` на
        `VoiceRecvClient` обрывает заодно и воспроизведение музыки, которым
        распознавание не владеет (см. докстринг `GuildPlayer._stop_playback_locked`).
        """
        async with self._lock:
            await self._detach_locked()

    async def _detach_locked(self) -> None:
        """Останавливает фоновые задачи всех говорящих и снимает приём звука с соединения."""
        for speaker_id in list(self._pump_tasks):
            await self._drop_speaker_locked(speaker_id)

        if self._voice_client is not None:
            try:
                self._voice_client.stop_listening()
            except Exception:
                logger.warning("Ошибка при остановке приёма голоса прослушивания", exc_info=True)
        self._voice_client = None

    def drop_speaker(self, speaker_id: int) -> None:
        """Убирает состояние говорящего, покинувшего голосовой канал — не дожидаясь таймаута.

        Не async и не блокирует вызывающий код: сама отмена фоновой задачи
        говорящего и освобождение его распознавателя планируются отдельной
        задачей на потоке event loop. Вызывать нужно из потока event loop
        (например, из обработчика `on_voice_state_update`, см.
        `bot.cogs.voice_control.VoiceControlCog`) — метод не потокобезопасен,
        в отличие от `_forward_from_discord_threadsafe`.
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
                if self._recorder is not None:
                    # Записывается PCM ровно в том виде, в каком он уходит
                    # в распознавание, — в этом весь смысл отладочной
                    # записи (см. модульный докстринг bot.speech_debug):
                    # запись «до» ресемплинга не ответила бы на вопрос,
                    # что именно слышала модель.
                    self._recorder.write(speaker_id, pcm)
                text = await self._recognizer.feed(speaker_id, pcm)
                if text:
                    if self._recorder is not None:
                        self._recorder.note_phrase(speaker_id, text)
                    await self._emit_phrase(speaker_id, text)

    async def _emit_phrase(self, speaker_id: int, text: str) -> None:
        """Отдаёт распознанную фразу колбэку; ошибки колбэка не роняют прослушивание."""
        if self._on_phrase is None:
            return
        try:
            await self._on_phrase(speaker_id, text)
        except Exception:
            logger.exception("Ошибка в колбэке распознанной фразы прослушивания")
