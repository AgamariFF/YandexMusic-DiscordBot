"""Ког голосового управления: постоянное распознавание речи и команды «Катя …».

Распознавание больше не запускается отдельной slash-командой — оно работает
всё время, пока бот подключён к голосовому каналу. Своего голосового
соединения у кога нет (см. модульный докстринг `bot.listening`): плеер
(`bot.player.GuildPlayer`) открывает и закрывает соединение сам, а этот ког
лишь подписан на два его хука — `on_player_voice_connected`/
`on_player_voice_disconnected` (см. `bot/__main__.py`, где они передаются в
конструктор `GuildPlayer` через `MusicCog`) — и по ним подключает либо
отключает `GuildListener` от того же самого соединения.

Чат-рулетка (`bot.roulette.GuildRoulette`) в эту схему не входит и остаётся
несовместимой и с волной, и с распознаванием — у неё своё отдельное
голосовое соединение (см. докстринг `bot.cogs.roulette.RouletteCog`), а её
запуск сам останавливает волну (`GuildPlayer.disconnect()`), что через тот
же хук `on_player_voice_disconnected` автоматически отключает и приём речи.

**Важно про приватность**: пока бот подключён к голосовому каналу,
распознавание слушает речь всех присутствующих постоянно, а не только пока
идёт какая-то команда — участников сервера нужно предупредить об этом
заранее (см. README, раздел про распознавание речи).

Распознанные фразы, адресованные боту («Катя, …», см. `bot.voice_commands`),
исполняются через `MusicCog.execute_voice_command`; всё остальное молча
игнорируется (см. `_process_phrase`), если не включён отладочный флаг
`SPEECH_TRANSCRIPT`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import discord
from discord.ext import commands, voice_recv

from bot.cogs.music import MusicCog
from bot.config import Config
from bot.errors import SpeechModelUnavailableError
from bot.listening import GuildListener
from bot.speech import SpeechRecognizer
from bot.speech_debug import SpeechRecorder
from bot.voice_commands import (
    VoiceCommand,
    describe_command,
    is_wake_word_only,
    parse_command_body,
    parse_voice_command,
)

logger = logging.getLogger(__name__)

# Сколько секунд после оклика по имени бот ждёт саму команду отдельной
# фразой (см. `_process_phrase`). Достаточно долго, чтобы человек успел
# договорить после паузы, и достаточно коротко, чтобы случайная реплика
# через минуту после «Катя?» уже не была принята за команду.
WAKE_FOLLOW_UP_SECONDS = 10.0

# Через сколько секунд убирается сообщение бота об выполненной голосовой
# команде: подтверждения нужны сразу и ненадолго, копить их в канале незачем.
_REPLY_LIFETIME_SECONDS = 10.0

# Команды, которые ходят в API Яндекс.Музыки и потому выполняются заметно
# дольше мгновенных: у них человек успевает решить, что бот не услышал (см.
# `VoiceControlCog._send_ack`).
_SLOW_ACTIONS = frozenset({"wave", "search", "skip"})


class VoiceControlCog(commands.Cog, name="Голосовое управление"):
    """Постоянное распознавание речи и выполнение голосовых команд «Катя …»."""

    def __init__(self, bot: commands.Bot, config: Config) -> None:
        """Создаёт единственные `SpeechRecognizer` и `GuildListener`, обслуживающие сервер бота.

        `MusicCog` сюда не передаётся, а связывается отдельно через
        `bind_music_cog`: `GuildPlayer` (созданный внутри `MusicCog`) должен
        получить хуки этого кога уже при собственном создании (см.
        `bot/__main__.py`), а самого `MusicCog` на момент создания этого
        кога ещё не существует — порядок создания на этом и упирается.
        """
        self._bot = bot
        self._config = config
        self._music_cog: MusicCog | None = None
        self._recognizer = SpeechRecognizer(model_path=config.speech_model_path)
        # Отладочная запись всего услышанного заводится только если задан
        # каталог (см. `bot.speech_debug` и предупреждение о приватности в
        # README): по умолчанию её нет вовсе, а не «есть, но выключена».
        self._recorder = (
            SpeechRecorder(
                config.speech_debug_dir, max_seconds=config.speech_debug_max_seconds
            )
            if config.speech_debug_dir
            else None
        )
        self._listener = GuildListener(
            recognizer=self._recognizer,
            on_phrase=self._handle_phrase,
            recorder=self._recorder,
        )
        self._model_unavailable_logged = False
        self._speech_disabled = False
        # Ссылки на фоновые задачи обработки фраз — см. докстринг `_handle_phrase`
        # про то, почему обработка не идёт прямо в колбэке, и `_process_phrase`
        # про сам дедлок. `asyncio` хранит на задачи только слабые ссылки:
        # без явного набора незавершённая задача могла бы быть собрана
        # сборщиком мусора раньше времени.
        self._background_tasks: set[asyncio.Task[None]] = set()
        # Кто окликнул бота по имени и до какого момента ждётся сама команда
        # отдельной фразой — см. `_process_phrase` и `WAKE_FOLLOW_UP_SECONDS`.
        self._awake_speaker: int | None = None
        self._awake_until: float = 0.0

    def bind_music_cog(self, music_cog: MusicCog) -> None:
        """Связывает ког с уже созданным `MusicCog` — нужен для выполнения голосовых команд."""
        self._music_cog = music_cog

    async def cog_unload(self) -> None:
        """Останавливает приём голоса, отладочную запись и фоновые задачи при выгрузке кога."""
        await self._listener.detach()
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._background_tasks.clear()
        if self._recorder is not None:
            await self._recorder.close()
        await self._recognizer.close()

    async def on_player_voice_connected(self, voice_client: voice_recv.VoiceRecvClient) -> None:
        """Хук `GuildPlayer.on_voice_connected`: подключает распознавание к соединению плеера.

        Если модели нет на диске, `GuildListener.attach` бросает
        `SpeechModelUnavailableError` (см. `SpeechRecognizer.ensure_ready`)
        — это не должно ломать музыку. Ошибка ловится здесь, логируется
        ОДИН раз за всё время работы процесса (а не на каждое подключение
        или переподключение к каналу), после чего распознавание навсегда
        отключается до следующего запуска бота. Бот без модели остаётся
        полностью рабочим музыкальным ботом.
        """
        if self._speech_disabled:
            return
        try:
            await self._listener.attach(voice_client)
            if self._recorder is not None:
                await self._recorder.start()
        except SpeechModelUnavailableError as exc:
            if not self._model_unavailable_logged:
                logger.warning(
                    "Голосовое управление отключено на время работы бота: %s", exc.user_message
                )
                self._model_unavailable_logged = True
            self._speech_disabled = True

    async def on_player_voice_disconnected(self) -> None:
        """Хук `GuildPlayer.on_voice_disconnected`: отключает распознавание перед разрывом связи."""
        await self._listener.detach()
        if self._recorder is not None:
            # Запись закрывается здесь, а не при выгрузке кога: именно
            # сейчас файлы дописываются и появляется итог по потерям —
            # иначе разбирать сеанс пришлось бы только после остановки бота.
            await self._recorder.close()

    def _resolve_speaker(self, speaker_id: int) -> discord.Member | None:
        """Определяет участника-говорящего по его id через голосовой канал прослушивания."""
        channel = self._listener.channel
        if channel is None:
            return None
        return channel.guild.get_member(speaker_id)

    def _target_channel(self) -> discord.abc.Messageable | None:
        """Текстовый канал для транскриптов и подтверждений голосовых команд.

        Своего текстового канала у голосового управления нет — раньше его
        явно указывала команда `/listen`, а постоянное распознавание
        запускается не командой. Используется канал последнего запуска
        волны/подключения (`MusicCog.announce_channel`).
        """
        if self._music_cog is None:
            return None
        return self._music_cog.announce_channel

    async def _publish_transcript(
        self, channel: discord.abc.Messageable, speaker_id: int, text: str
    ) -> None:
        """Публикует распознанную фразу в текстовый канал — отладка за флагом SPEECH_TRANSCRIPT."""
        member = self._resolve_speaker(speaker_id)
        name = member.display_name if member is not None else f"пользователь {speaker_id}"
        await channel.send(
            f"**{name}:** {text}",
            # text — распознанная речь, свободный ввод чужого человека, а не
            # самого бота; без явного none() упоминание роли или @everyone
            # внутри фразы ушло бы как настоящий пинг от имени бота (в
            # проекте уже была такая уязвимость, см.
            # bot.cogs.music._search_and_start).
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _handle_phrase(self, speaker_id: int, text: str) -> None:
        """Колбэк `GuildListener.on_phrase`: планирует обработку фразы отдельной задачей event loop.

        Фактическая обработка (`_process_phrase`) не выполняется прямо
        здесь и намеренно: этот колбэк вызывается изнутри фоновой задачи
        `GuildListener._pump_speaker` конкретного говорящего (см. докстринг
        `GuildListener`, который трогать нельзя). Голосовая команда
        «отключись» ведёт к `GuildPlayer.disconnect()` → хуку
        `on_player_voice_disconnected` → `GuildListener.detach()`, который
        отменяет фоновые задачи ВСЕХ говорящих — включая ту самую задачу,
        что сейчас обрабатывала бы эту же фразу. Отмена и ожидание самой
        себя в одном стеке вызовов — гарантированный дедлок, поэтому вся
        обработка фразы идёт отдельной, не связанной со стеком `_pump_speaker`
        задачей.
        """
        task = asyncio.create_task(self._process_phrase(speaker_id, text))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(self._log_process_phrase_result)

    @staticmethod
    def _log_process_phrase_result(task: asyncio.Task[None]) -> None:
        """Логирует исключение из фоновой обработки голосовой фразы, если оно возникло."""
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Ошибка при обработке распознанной голосовой фразы")

    def _parse_phrase(self, speaker_id: int, text: str) -> VoiceCommand | None:
        """Разбирает фразу в команду, учитывая оклик по имени отдельной фразой.

        Vosk режет речь на фразы по паузам, а обращение «Катя, следующий»
        люди произносят именно с паузой после имени — поэтому команда сплошь
        и рядом приезжает двумя фразами подряд: «катя», затем «следующее».
        Без этого метода вторая половина не имела бы обращения и молча
        отбрасывалась бы (именно так и выглядела жалоба «команды срабатывают
        не всегда»). Поэтому голый оклик по имени открывает короткое окно
        (`WAKE_FOLLOW_UP_SECONDS`), в течение которого СЛЕДУЮЩАЯ фраза ТОГО
        ЖЕ человека принимается как команда без повторного имени.

        Окно закрывается первой же следующей фразой этого человека —
        неважно, оказалась она командой или нет. Иначе одинокое «Катя?»
        превратило бы в команды весь его дальнейший разговор на десять
        секунд вперёд.
        """
        command = parse_voice_command(text)
        if command is not None:
            self._close_wake_window()
            return command

        if is_wake_word_only(text):
            self._awake_speaker = speaker_id
            self._awake_until = time.monotonic() + WAKE_FOLLOW_UP_SECONDS
            logger.debug(
                "Оклик по имени от говорящего %s — жду команду отдельной фразой %.0f с",
                speaker_id,
                WAKE_FOLLOW_UP_SECONDS,
            )
            return None

        if self._awake_speaker != speaker_id or time.monotonic() >= self._awake_until:
            return None

        self._close_wake_window()
        return parse_command_body(text)

    def _close_wake_window(self) -> None:
        """Закрывает окно ожидания команды после оклика по имени."""
        self._awake_speaker = None
        self._awake_until = 0.0

    async def _process_phrase(self, speaker_id: int, text: str) -> None:
        """Обрабатывает распознанную фразу: исполняет команду «Катя …», остальное молча пропускает.

        Раньше (команда /listen) в текстовый канал уходила КАЖДАЯ
        распознанная фраза — теперь распознавание работает постоянно, и
        такой поток был бы и спамом, и сливом чужих разговоров. Поэтому по
        умолчанию в чат ничего не публикуется, только `logger.debug`. Полная
        публикация всех фраз остаётся отладочной возможностью за флагом
        `SPEECH_TRANSCRIPT` (см. `bot.config.Config.speech_transcript`).
        """
        channel = self._target_channel()
        if self._config.speech_transcript and channel is not None:
            await self._publish_transcript(channel, speaker_id, text)

        command = self._parse_phrase(speaker_id, text)
        if command is None:
            logger.debug("Распознанная фраза не является голосовой командой: %r", text)
            return

        if self._music_cog is None:
            return

        speaker = self._resolve_speaker(speaker_id)
        ack = await self._send_ack(channel, command)
        reply = await self._music_cog.execute_voice_command(command, speaker)
        await self._finish_ack(channel, ack, reply)

    async def _send_ack(
        self, channel: discord.abc.Messageable | None, command: VoiceCommand
    ) -> discord.Message | None:
        """Подтверждает, что команда услышана, ДО того как она будет выполнена.

        Запуск волны, поиск трека и переключение ходят в API Яндекс.Музыки
        и занимают заметное время. Без этого подтверждения всё это время
        непонятно, услышал бот команду или нет, и человек повторяет её
        вслух — а бот потом выполняет обе. Поэтому сначала быстрый ответ
        «слышу, делаю то-то», и только потом сама работа.

        Мгновенные команды (пауза, продолжение) подтверждать отдельно
        незачем — они и так ответят раньше, чем подтверждение долетит.
        """
        if channel is None or command.action not in _SLOW_ACTIONS:
            return None
        try:
            return await channel.send(
                f"⏳ Слышу: {describe_command(command)}…",
                # Описание включает свободный ввод из самой команды
                # (поисковый запрос) — тот же приём и та же причина, что и
                # в _publish_transcript выше.
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            # Подтверждение — удобство, а не часть команды: не смогли его
            # отправить — выполняем команду всё равно.
            logger.debug("Не удалось отправить подтверждение голосовой команды", exc_info=True)
            return None

    async def _finish_ack(
        self,
        channel: discord.abc.Messageable | None,
        ack: discord.Message | None,
        reply: str,
    ) -> None:
        """Заменяет подтверждение итогом команды, либо отправляет итог заново."""
        mentions = discord.AllowedMentions.none()
        if ack is not None:
            try:
                await ack.edit(content=reply, allowed_mentions=mentions)
                await ack.delete(delay=_REPLY_LIFETIME_SECONDS)
                return
            except discord.HTTPException:
                logger.debug("Не удалось обновить подтверждение команды", exc_info=True)
        if channel is not None:
            await channel.send(
                reply,
                delete_after=_REPLY_LIFETIME_SECONDS,
                allowed_mentions=mentions,
            )

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Убирает распознаватель участника, покинувшего прослушиваемый канал.

        Не дожидаясь таймаута бездействия: без этого обработчика память
        ушедшего участника освободилась бы только фоновым таймаутом
        `SpeechRecognizer` (см. его докстринг про `speaker_idle_timeout`) —
        здесь причина освобождения известна точно и сразу.
        """
        channel = self._listener.channel
        if channel is None or before.channel is None or before.channel.id != channel.id:
            return
        if after.channel is not None and after.channel.id == channel.id:
            return
        self._listener.drop_speaker(member.id)
