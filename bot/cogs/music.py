"""Ког со slash-командами управления «Моей волной» Яндекс.Музыки."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands, voice_recv

from bot.audio.bassboost import BassLevel
from bot.cogs.player_view import PlayerView
from bot.cogs.views import MAX_SEARCH_RESULTS, TrackSearchView, build_search_embed
from bot.config import Config
from bot.errors import (
    BotError,
    NothingPlayingError,
    NothingToSayError,
    NotInVoiceChannelError,
    SpeechSynthesisUnavailableError,
)
from bot.player import GuildPlayer, PlayerState
from bot.tts import MAX_TEXT_LENGTH, TextToSpeech, clean_text
from bot.voice_commands import VoiceCommand
from bot.yandex import TrackInfo, YandexMusicClient

logger = logging.getLogger(__name__)

EMBED_COLOR = discord.Color.gold()

_BASS_CHOICES = [app_commands.Choice(name=level.label, value=level.value) for level in BassLevel]


def format_duration(seconds: float) -> str:
    """Форматирует длительность как M:SS, либо H:MM:SS для длительностей от часа."""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def build_player_embed(player: GuildPlayer, color: discord.Color) -> discord.Embed:
    """Собирает embed единственного сообщения-плеера по текущему состоянию `GuildPlayer`.

    Единственное место, где строится вид плеера: и колбэки `_announce`/
    `_handle_stopped`, и каждая кнопка `PlayerView` перерисовывают сообщение
    через эту же функцию, поэтому оно не может разойтись само с собой.

    Порядок в embed'е сверху вниз — author → title → большая картинка →
    footer. Обложка ставится большой картинкой (`set_image`), а Discord не
    даёт разместить произвольный текст НИЖЕ такой картинки — только footer.
    Раз по ТЗ именно там (под обложкой) должны быть исполнитель и название,
    footer — единственное подходящее место; это осознанный компромисс
    структуры embed'а, а не недосмотр.

    Полосы прогресса здесь намеренно нет: сообщение перерисовывается только
    по событиям (смена трека, нажатия кнопок), а не по таймеру. "Убегающий"
    таймер в такой схеме всегда показывал бы 0:00 — секундная точность
    потребовала бы постоянно править сообщение и упёрлась бы в лимиты
    Discord на частоту правок. Поэтому в footer — только статичная общая
    длительность трека, которая обновления не требует.
    """
    track = player.current
    info = None
    if track is not None:
        try:
            info = player.now_playing()
        except NothingPlayingError:
            # Гонка: трек мог закончиться между чтением player.current и этим
            # вызовом (кнопка читает состояние не под локом плеера). В этом
            # случае просто показываем "остановлена", а не падаем.
            track = None

    if track is None or info is None:
        return discord.Embed(
            title="«Моя волна» остановлена",
            description="Воспроизведение завершено.",
            color=color,
        )

    paused = player.state is PlayerState.PAUSED
    embed = discord.Embed(title=player.wave_description or "Моя волна", color=color)
    embed.set_author(name="На паузе" if paused else "Сейчас играет")

    if track.cover_url is not None:
        embed.set_image(url=track.cover_url)

    footer_text = f"{track.artists} — {track.title}"
    if track.duration > 0:
        footer_text = f"{footer_text} • {format_duration(track.duration)}"
    embed.set_footer(text=footer_text)
    return embed


def _voice_channel_of(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel:
    """Возвращает голосовой канал участника, вызвавшего команду."""
    user = interaction.user
    if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
        raise NotInVoiceChannelError()
    return user.voice.channel


class MusicCog(commands.Cog, name="Музыка"):
    """Slash-команды управления «Моей волной»."""

    def __init__(
        self,
        bot: commands.Bot,
        config: Config,
        client: YandexMusicClient,
        *,
        on_voice_connected: Callable[[voice_recv.VoiceRecvClient], Awaitable[None]] | None = None,
        on_voice_disconnected: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Создаёт единственный GuildPlayer, обслуживающий сервер бота.

        `on_voice_connected`/`on_voice_disconnected` пробрасываются прямо в
        `GuildPlayer` — этот ког лишь передаточное звено между
        `bot/__main__.py` (где создаётся `bot.cogs.voice_control.VoiceControlCog`
        и его хуки) и конструктором плеера, у которого голосовое соединение
        открывается и закрывается на самом деле. Подробности — в докстринге
        `GuildPlayer.__init__`.
        """
        self._bot = bot
        self._config = config
        self._client = client
        self._announce_channel: discord.abc.Messageable | None = None
        # Синтез речи создаётся всегда, но модель грузит только при первой
        # произнесённой фразе (см. bot.tts) — выключенная настройка просто
        # не даёт до него дойти.
        self._tts = TextToSpeech(
            model_name=config.tts_model_name, speaker_id=config.tts_speaker_id
        )
        # Единственное сообщение-плеер сервера, его текущий PlayerView и лок,
        # под которым идёт любая правка — подробности см. в докстринге
        # `_update_player_message`. `_player_view` нужен отдельно от
        # `_player_message`, чтобы `_replace_player_view` могла остановить
        # предыдущий экземпляр вью (см. её докстринг про рост ViewStore).
        self._player_message: discord.Message | discord.InteractionMessage | None = None
        self._player_view: PlayerView | None = None
        self._player_message_lock = asyncio.Lock()
        self._player = GuildPlayer(
            client,
            ffmpeg_path=config.ffmpeg_path,
            default_volume=config.default_volume,
            idle_timeout=config.idle_timeout,
            announce=self._announce,
            on_stopped=self._handle_stopped,
            on_voice_connected=on_voice_connected,
            on_voice_disconnected=on_voice_disconnected,
        )

    @property
    def player(self) -> GuildPlayer:
        """Единственный проигрыватель волны, обслуживающий сервер бота."""
        return self._player

    @property
    def announce_channel(self) -> discord.abc.Messageable | None:
        """Текстовый канал последнего запуска волны/подключения, либо None.

        Нужен `bot.cogs.voice_control.VoiceControlCog`: у голосовых команд
        нет своего текстового канала (в отличие от slash-команд с их
        `interaction.channel`), транскрипты и подтверждения голосовых
        команд идут туда же, куда обычно уходят объявления о новом треке.
        """
        return self._announce_channel

    async def cog_unload(self) -> None:
        """Останавливает текущий PlayerView и отключает плеер при выгрузке кога."""
        if self._player_view is not None:
            self._player_view.stop()
        await self._player.disconnect()

    async def _announce(self, track: TrackInfo, wave_description: str | None) -> None:
        """Отражает старт нового трека в сообщении-плеере.

        `track` и `wave_description` не читаются напрямую: `_update_player_message`
        берёт актуальное состояние `self._player` в момент вызова. Расхождения
        не будет — `_play_track_locked` обновляет состояние плеера ДО вызова
        этого колбэка (см. докстринг `GuildPlayer`).

        `reposition=True`: старт трека — это и есть "смена трека" из ТЗ на
        пересоздание (см. докстринг `_update_player_message`). Колбэк
        срабатывает одинаково и при обычном автопереходе, и при запуске
        волны из /wave, /search или кнопки «Следующий» — GuildPlayer не
        различает эти причины, и это осознанно: если к моменту старта трека
        плеер оказался погребён под перепиской, его стоит поднять в любом
        из этих случаев.
        """
        await self._update_player_message(reposition=True)

    async def _handle_stopped(self) -> None:
        """Отражает остановку волны в сообщении-плеере — колбэк `GuildPlayer.on_stopped`.

        Срабатывает событийно (см. `GuildPlayer._stop_locked_state`), а не по
        опросу: `on_stopped` гарантирует однократность и молчит, если волна и
        не запускалась, поэтому здесь достаточно просто перерисовать сообщение.
        Остановка волны не входит в список событий, требующих пересоздания
        (см. `_update_player_message`), — правим на месте.
        """
        await self._update_player_message()

    async def _update_player_message(
        self, interaction: discord.Interaction | None = None, *, reposition: bool = False
    ) -> None:
        """Обновляет сообщение-плеер на месте, либо отправляет новое, если старого не осталось.

        Единственная точка правки сообщения-плеера: сюда идут и колбэк
        `_announce` (фоновая задача плеера), и все кнопки `PlayerView` —
        оба источника могут сработать почти одновременно (например,
        автопереход на следующий трек и нажатие «Следующий»), поэтому вся
        работа с `self._player_message` идёт под одним `_player_message_lock`.
        Без него конкурентные правки могли бы примениться в непредсказуемом
        порядке и оставить в сообщении устаревший трек.

        Если передан `interaction` с ещё не использованным `response` —
        используется `edit_message`, тот самый механизм правки "на месте" по
        нажатию кнопки. Если сообщение, к которому эта кнопка была
        прикреплена, успели удалить вручную, `edit_message` упадёт
        `discord.NotFound` — в этом случае, как и когда сохранённого
        сообщения ещё/уже нет, отправляется новое и запоминается.

        `reposition` — явный параметр, а не догадка по контексту (в чате
        неоткуда достоверно узнать, "значимое" ли это событие, кроме как
        спросив вызывающий код). `True` просят только источники значимых
        событий — старт трека (`_announce`), который покрывает в том числе
        и запуск волны после выбора в поиске: тогда, если плеер успел
        оказаться не последним сообщением канала (`не self._player_message_is_last()`),
        старое сообщение удаляется и отправляется новое — оно и станет
        последним. При обычных нажатиях кнопок (пауза, следующий, отключить)
        параметр остаётся `False`, и сообщение правится на месте: если бы
        каждое нажатие могло пересоздавать сообщение, активная переписка в
        канале заставляла бы бота бесконечно перевыкладывать плеер и спамить.

        Каждый успешный путь заканчивается вызовом `_replace_player_view` —
        она же и фиксирует, каким сообщением/вью сейчас владеет ког.
        """
        async with self._player_message_lock:
            embed = build_player_embed(self._player, EMBED_COLOR)
            view = PlayerView(player=self._player, controller=self)
            mentions = discord.AllowedMentions.none()

            if interaction is not None and not interaction.response.is_done():
                try:
                    await interaction.response.edit_message(
                        embed=embed, view=view, allowed_mentions=mentions
                    )
                    # edit_message правит именно то сообщение, к которому
                    # прикреплена нажатая кнопка, — это и есть interaction.message.
                    self._replace_player_view(view, message=interaction.message)
                    return
                except discord.NotFound:
                    # Сообщение с кнопкой удалили вручную; response ещё не
                    # израсходован (edit_message не дошёл до сети) — упадём в
                    # отправку нового ниже, ответив тем же взаимодействием.
                    pass

            if (interaction is None or interaction.response.is_done()) and (
                self._player_message is not None
            ):
                if reposition and not self._player_message_is_last():
                    # Плеер погребён под более новыми сообщениями — редактировать
                    # его на месте бессмысленно, он всё равно останется не
                    # последним. Удаляем и падаем ниже, в отправку нового.
                    await self._delete_player_message()
                else:
                    try:
                        await self._player_message.edit(
                            embed=embed, view=view, allowed_mentions=mentions
                        )
                        self._replace_player_view(view, message=self._player_message)
                        return
                    except discord.NotFound:
                        self._player_message = None

            if interaction is not None:
                if interaction.response.is_done():
                    message = await interaction.followup.send(
                        embed=embed, view=view, allowed_mentions=mentions, wait=True
                    )
                else:
                    await interaction.response.send_message(
                        embed=embed, view=view, allowed_mentions=mentions
                    )
                    message = await interaction.original_response()
                self._replace_player_view(view, message=message)
                return

            if self._announce_channel is None:
                return
            message = await self._announce_channel.send(
                embed=embed, view=view, allowed_mentions=mentions
            )
            self._replace_player_view(view, message=message)

    def _player_message_is_last(self) -> bool:
        """Проверяет, остаётся ли `self._player_message` последним сообщением своего канала.

        Дешёвая проверка без похода в историю канала: `channel.last_message_id`
        уже закеширован клиентом по гейтвею при каждом новом сообщении, и
        сравнение с ним не стоит отдельного HTTP-запроса. Вызывается только
        когда `self._player_message` уже не `None` (см. `_update_player_message`),
        но на случай нетипичного канала без атрибута `last_message_id`
        считаем, что плеер НЕ последний, — лучше лишний раз пересоздать
        сообщение, чем оставить его молча погребённым под перепиской.
        """
        message = self._player_message
        if message is None:
            return True
        last_id = getattr(message.channel, "last_message_id", None)
        if last_id is None:
            return False
        return last_id == message.id

    async def _delete_player_message(self) -> None:
        """Удаляет устаревшее сообщение-плеер перед тем, как отправить новое взамен.

        Вызывается только когда `_update_player_message` уже решила
        пересоздать сообщение (см. её докстринг про `reposition`). Удаление
        может не получиться: `discord.NotFound` — сообщение уже удалили
        (вручную или другим путём), `discord.Forbidden` — у бота нет права
        удалять сообщения в этом канале. Ни то, ни другое не должно прерывать
        обновление плеера — просто забываем о старом сообщении и создаём
        новое ниже по `_update_player_message`.
        """
        message = self._player_message
        self._player_message = None
        if message is None:
            return
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            logger.warning("Не удалось удалить устаревшее сообщение-плеер при пересоздании")

    def _replace_player_view(
        self, view: PlayerView, *, message: discord.Message | discord.InteractionMessage
    ) -> None:
        """Запоминает новые сообщение и view, останавливая предыдущий view.

        Новый `PlayerView` пересоздаётся при каждом обновлении (см. докстринг
        класса), а его кнопки получают случайные `custom_id`. discord.py
        хранит соответствие "сообщение → активные компоненты" во внутреннем
        `ViewStore`, и при каждой правке с `view=...` добавляет туда записи
        нового вью, но НЕ убирает записи предыдущего — они с другими
        `custom_id`, и раз это не единый персистентный вью с фиксированными
        `custom_id`, стору просто неоткуда узнать, что старый экземпляр уже
        никому не нужен. Без явной остановки они копились бы там вечно, пока
        жива волна. `View.stop()` как раз и вызывает `ViewStore.remove_view`,
        снимая записи именно старого экземпляра.
        """
        old_view = self._player_view
        self._player_message = message
        self._player_view = view
        if old_view is not None:
            old_view.stop()

    async def handle_pause_toggle(self, interaction: discord.Interaction) -> None:
        """Переключает паузу/воспроизведение по кнопке плеера и правит сообщение на месте.

        `pause`/`resume` синхронны и не ходят в сеть, поэтому вместо
        `defer()` сразу используется `edit_message` через
        `_update_player_message` — ответ на нажатие и есть обновлённое
        сообщение.
        """
        if self._player.state is PlayerState.PAUSED:
            self._player.resume()
        else:
            self._player.pause()
        await self._update_player_message(interaction)

    async def handle_skip(self, interaction: discord.Interaction) -> None:
        """Пропускает трек по кнопке плеера.

        `skip()` резолвит следующий трек в сети и может занять больше трёх
        секунд, поэтому сначала откладываем ответ. Сообщение поправит либо
        колбэк `_announce` нового трека (он срабатывает прямо внутри
        `skip()`), либо явный вызов ниже — если волна на этом закончилась и
        анонса не будет.
        """
        await interaction.response.defer()
        await self._player.skip()
        await self._update_player_message(interaction)

    async def handle_disconnect(self, interaction: discord.Interaction) -> None:
        """Отключает плеер по кнопке — сообщение перейдёт в состояние "завершено"."""
        await interaction.response.defer()
        await self._player.disconnect()
        await self._update_player_message(interaction)

    async def handle_search_query(self, interaction: discord.Interaction, query: str) -> None:
        """Обрабатывает запрос из модального окна поиска — та же ветка, что и у /search."""
        await interaction.response.defer(thinking=True)
        await self._search_and_start(interaction, query)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Разрешает команды кога только на сконфигурированном сервере Discord."""
        if interaction.guild_id != self._config.guild_id:
            await interaction.response.send_message(
                "Эта команда недоступна на этом сервере.", ephemeral=True
            )
            return False
        return True

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        """Единый обработчик ошибок slash-команд: безопасный текст пользователю, полный лог."""
        original = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        command_name = interaction.command.qualified_name if interaction.command else "?"

        if isinstance(original, app_commands.CheckFailure):
            # interaction_check уже отправил пользователю сообщение и вернул False.
            return

        if isinstance(original, BotError):
            logger.warning("Ошибка команды /%s: %s", command_name, original)
            await self._send_error(interaction, original.user_message)
            return

        logger.error("Необработанная ошибка команды /%s", command_name, exc_info=original)
        await self._send_error(interaction, "Внутренняя ошибка, подробности в логах.")

    @staticmethod
    async def _send_error(interaction: discord.Interaction, text: str) -> None:
        """Отправляет текст ошибки с учётом того, был ли ответ уже начат (в т.ч. отложен)."""
        if interaction.response.is_done():
            try:
                await interaction.edit_original_response(content=text)
            except discord.HTTPException:  # NotFound — её подкласс
                await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _ensure_connected(self, interaction: discord.Interaction) -> None:
        """Подключается к каналу вызвавшего пользователя, если плеер ещё не в канале.

        Общая часть `_start_wave` и `_start_wave_from_track`: обе запускают
        волну «на живую» и должны заходить в голосовой канал ровно в момент
        старта, а не раньше (иначе бот молча зайдёт в канал даже тогда, когда
        волна так и не запустится, например пользователь не выбрал трек из
        меню поиска).
        """
        if self._player.voice_client is None:
            channel = _voice_channel_of(interaction)
            await self._player.connect(channel)
        self._announce_channel = interaction.channel

    async def _send_started_ack(self, interaction: discord.Interaction) -> None:
        """Подтверждает запуск волны, правя исходный ответ, а не отправляя новый.

        Раньше команды слали публичный embed «Моя волна запущена» отдельным
        сообщением на каждый запуск, а `_announce` — ещё один embed на
        каждый трек: получался спам параллельно с сообщением-плеером. Теперь
        сам плеер — единственное публичное сообщение (его к этому моменту
        уже обновил колбэк `_announce`), а на взаимодействие всё равно нужно
        ответить.

        Раньше здесь стоял `interaction.followup.send(..., ephemeral=True)`.
        Проблема: `/search` и модальное окно поиска откладывают ответ
        публично (`defer()`/`defer(thinking=True)`, без `ephemeral=True`) —
        Discord показывает всем «Бот думает…» как настоящее сообщение в
        канале. Эфемерный followup его никак не резолвит: приватное
        сообщение уходило одному пользователю, а публичный плейсхолдер так и
        оставался висеть нерешённым. `edit_original_response` правит именно
        тот самый первый ответ — тот же приём, что и в `_send_error` — поэтому
        плейсхолдер всегда получает финальный текст, каким бы он ни был
        отложен: публичным (тогда и подтверждение публичное) или эфемерным
        (`_start_wave` — тогда и подтверждение эфемерное). `embed`/`view`
        сбрасываются явно: если ответ уже редактировался раньше (например,
        `TrackSearchView.handle_selection` подставила туда меню выбора), от
        него не должно остаться следов.
        """
        text = "«Моя волна» запущена — смотрите сообщение-плеер в канале."
        if interaction.response.is_done():
            try:
                await interaction.edit_original_response(content=text, embed=None, view=None)
            except discord.HTTPException:  # NotFound — её подкласс
                await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def _start_wave(self, interaction: discord.Interaction) -> None:
        """Запускает «Мою волну», при необходимости подключаясь к каналу пользователя."""
        await interaction.response.defer(ephemeral=True)
        await self._ensure_connected(interaction)
        await self._player.start_wave()
        await self._send_started_ack(interaction)

    async def _start_wave_from_track(
        self, interaction: discord.Interaction, track: TrackInfo
    ) -> None:
        """Запускает волну от выбранного трека: /search, меню выбора и поиск с кнопки плеера.

        Подходит для обоих случаев ответа: и когда `interaction.response` ещё
        не использован (одиночный результат поиска, после `defer()`), и когда
        он уже израсходован на `edit_message` (выбор из меню) — `_send_started_ack`
        сама решает, как ответить в каждом случае.
        """
        await self._ensure_connected(interaction)
        await self._player.start_wave_from_track(track)
        await self._send_started_ack(interaction)

    async def _search_tracks(self, query: str) -> list[TrackInfo]:
        """Ищет треки — общая часть /search, модального окна поиска и голосовых команд."""
        return await self._client.search_tracks(query, limit=MAX_SEARCH_RESULTS)

    async def _search_and_start(self, interaction: discord.Interaction, query: str) -> None:
        """Ищет треки и либо сразу запускает волну, либо показывает меню выбора.

        Общая ветка (пусто/один/несколько) для команды /search и модального
        окна поиска на кнопке плеера — чтобы не дублировать её в двух местах.
        Требует, чтобы `interaction.response` был уже отложен (`defer`)
        вызывающим кодом.
        """
        tracks = await self._search_tracks(query)

        if not tracks:
            await interaction.followup.send(
                f"Ничего не найдено по запросу «{query}».",
                # query — свободный текст пользователя, бот создан без глобального
                # allowed_mentions (см. bot/__main__.py), поэтому без явного none()
                # упоминание роли или @everyone внутри запроса ушло бы как
                # настоящий пинг от имени бота.
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        if len(tracks) == 1:
            await self._start_wave_from_track(interaction, tracks[0])
            return

        view = TrackSearchView(
            tracks=tracks, author_id=interaction.user.id, on_select=self._start_wave_from_track
        )
        message = await interaction.followup.send(
            embed=build_search_embed(query, tracks, EMBED_COLOR),
            # См. комментарий выше про allowed_mentions: query подставляется в
            # текст embed'а. Discord и так не резолвит упоминания внутри
            # embed'ов в пинги, но параметр держим — это дешёвая защита,
            # которую однажды уже пришлось вернуть после регресса.
            allowed_mentions=discord.AllowedMentions.none(),
            view=view,
            wait=True,
        )
        view.attach_message(message)

    # --- Голосовые команды («Катя …», см. bot.voice_commands) --------------
    # Диспетчеризация живёт здесь, а не в bot.cogs.voice_control.VoiceControlCog:
    # этот ког уже владеет и плеером, и сообщением-плеером, и существующими
    # путями поиска/запуска волны, переиспользуемыми и голосовыми командами.

    _VOICE_COMMANDS_UPDATE_PLAYER_MESSAGE = frozenset(
        {"pause", "resume", "skip", "stop", "wave", "search", "volume"}
    )

    async def execute_voice_command(
        self, command: VoiceCommand, speaker: discord.Member | None
    ) -> str:
        """Выполняет голосовую команду «Катя …» и возвращает короткий текст-подтверждение.

        Доменные ошибки (`bot.errors.BotError`, например `NothingPlayingError`
        на «пауза», когда ничего не играет) перехватываются здесь и
        превращаются в их же `user_message` — голосовая команда не должна
        ронять распознавание речи исключением. После любого действия,
        меняющего состояние плеера (`_VOICE_COMMANDS_UPDATE_PLAYER_MESSAGE`),
        сообщение-плеер обновляется тем же `_update_player_message()`, что и
        кнопки/slash-команды, только без интеракции — её у голосовой команды
        нет.
        """
        try:
            reply = await self._dispatch_voice_command(command, speaker)
        except BotError as exc:
            logger.warning("Ошибка голосовой команды %s: %s", command.action, exc)
            return exc.user_message
        if command.action in self._VOICE_COMMANDS_UPDATE_PLAYER_MESSAGE:
            await self._update_player_message()
        return reply

    async def _dispatch_voice_command(
        self, command: VoiceCommand, speaker: discord.Member | None
    ) -> str:
        """Сопоставляет действие голосовой команды с уже существующим API `GuildPlayer`."""
        if command.action == "pause":
            self._player.pause()
            return "Воспроизведение приостановлено."
        if command.action == "resume":
            self._player.resume()
            return "Воспроизведение возобновлено."
        if command.action == "skip":
            next_track = await self._player.skip()
            if next_track is None:
                return "Трек пропущен."
            return f"Трек пропущен. Далее: {next_track.display}"
        if command.action == "stop":
            await self._player.disconnect()
            return "Отключился от голосового канала."
        if command.action == "wave":
            if command.query is None:
                error = await self._connect_for_speaker(speaker)
                if error is not None:
                    return error
                await self._player.start_wave()
                return "«Моя волна» запущена."
            return await self._search_and_start_wave_voice(command.query, speaker)
        if command.action == "repeat":
            error = await self._connect_for_speaker(speaker)
            if error is not None:
                return error
            spoken = await self.say_text(command.query or "")
            return f"Сказала: «{spoken}»"
        if command.action == "search":
            return await self._search_and_start_wave_voice(command.query or "", speaker)
        if command.action == "volume":
            return self._apply_voice_volume(command)
        if command.action == "now_playing":
            info = self._player.now_playing()
            elapsed = format_duration(info.elapsed)
            total = format_duration(info.track.duration)
            return f"Сейчас играет: {info.track.display} ({elapsed} / {total})."
        raise AssertionError(f"Неизвестное действие голосовой команды: {command.action}")

    async def _search_and_start_wave_voice(
        self, query: str, speaker: discord.Member | None
    ) -> str:
        """Общая часть действий `search` и `wave` (с query): ищет трек и запускает волну от него.

        Голосовой аналог `_search_and_start`/`_start_wave_from_track`, но
        без интеракции: `search` и `wave` с заданным треком/исполнителем
        (`command.query`) делают одно и то же — находят трек и запускают
        волну от него.
        """
        track = await self._resolve_search_track(query)
        if track is None:
            return f"Ничего не найдено по запросу «{query}»."
        error = await self._connect_for_speaker(speaker)
        if error is not None:
            return error
        await self._player.start_wave_from_track(track)
        return f"«Моя волна» по {track.artists} — {track.title} запущена."

    async def _resolve_search_track(self, query: str) -> TrackInfo | None:
        """Ищет треки по запросу и возвращает самый подходящий, либо None, если ничего не нашлось.

        У голосовых команд нет интеракции, к которой можно привязать меню
        выбора из нескольких найденных треков (см. `TrackSearchView`),
        поэтому при нескольких результатах просто берётся первый — тот же
        трек, что стал бы единственным пунктом меню.
        """
        tracks = await self._search_tracks(query)
        return tracks[0] if tracks else None

    async def _connect_for_speaker(self, speaker: discord.Member | None) -> str | None:
        """Подключается к каналу говорящего для голосовой команды, если плеер ещё не в канале.

        Голосовой аналог `_ensure_connected(interaction)`: там канал берётся
        из `interaction.user`, здесь — из `speaker`, единственного
        "заявителя" фразы, которого знает вызывающий код (см.
        `bot.cogs.voice_control.VoiceControlCog._resolve_speaker`). В отличие
        от `_ensure_connected`, `self._announce_channel` не трогается — у
        голосовой команды нет своего текстового канала (см.
        `bot.cogs.voice_control.VoiceControlCog._target_channel`),
        устанавливать его как канал объявлений было бы неверно. Возвращает
        текст ошибки, если подключиться невозможно, иначе None.
        """
        if self._player.voice_client is not None:
            return None
        if speaker is None or speaker.voice is None or speaker.voice.channel is None:
            return "Не могу подключиться: вы должны находиться в голосовом канале."
        await self._player.connect(speaker.voice.channel)
        return None

    def _apply_voice_volume(self, command: VoiceCommand) -> str:
        """Применяет абсолютную либо относительную громкость голосовой команды `volume`."""
        if command.volume_percent is not None:
            new_volume = self._player.set_volume(command.volume_percent / 100)
        elif command.volume_delta is not None:
            new_volume = self._player.set_volume(self._player.volume + command.volume_delta / 100)
        else:
            return f"Текущая громкость: {round(self._player.volume * 100)}%."
        return f"Громкость установлена: {round(new_volume * 100)}%."

    @app_commands.command(name="join", description="Подключиться к голосовому каналу")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        """Подключает бота к голосовому каналу пользователя, вызвавшего команду."""
        await interaction.response.defer()
        channel = _voice_channel_of(interaction)
        await self._player.connect(channel)
        await interaction.followup.send(f"Подключился к каналу «{channel.name}».")

    @app_commands.command(name="leave", description="Отключиться от голосового канала")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        """Отключает бота от голосового канала."""
        await interaction.response.defer()
        await self._player.disconnect()
        await interaction.followup.send("Отключился от голосового канала.")

    @app_commands.command(name="wave", description="Запустить «Мою волну»")
    @app_commands.guild_only()
    async def wave(self, interaction: discord.Interaction) -> None:
        """Запускает «Мою волну», при необходимости подключаясь к каналу пользователя."""
        await self._start_wave(interaction)

    @app_commands.command(name="play", description="Запустить «Мою волну»")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction) -> None:
        """Запускает «Мою волну», при необходимости подключаясь к каналу пользователя."""
        await self._start_wave(interaction)

    @app_commands.command(name="search", description="Найти трек и запустить «Мою волну» от него")
    @app_commands.describe(query="Что искать: исполнитель и/или название трека")
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        """Ищет треки по запросу: один найденный — сразу волна от него, несколько — меню выбора."""
        await interaction.response.defer()
        await self._search_and_start(interaction, query)

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        """Пропускает текущий воспроизводимый трек и синхронизирует сообщение-плеер."""
        await interaction.response.defer()
        next_track = await self._player.skip()
        if next_track is None:
            await interaction.followup.send("Трек пропущен.")
        else:
            await interaction.followup.send(f"Трек пропущен. Далее: {next_track.display}")
        # Как и кнопка «Следующий»: `skip()` уже мог обновить сообщение-плеер
        # сам (колбэк `_announce`/`_handle_stopped` внутри него), но этот
        # вызов — гарантия, что оно точно не останется рассинхронизировано с
        # тем, что команда только что сделала.
        await self._update_player_message(interaction)

    @app_commands.command(name="pause", description="Поставить воспроизведение на паузу")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        """Ставит текущее воспроизведение на паузу и синхронизирует сообщение-плеер."""
        self._player.pause()
        await interaction.response.send_message("Воспроизведение приостановлено.")
        await self._update_player_message(interaction)

    @app_commands.command(name="resume", description="Возобновить воспроизведение")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        """Возобновляет воспроизведение после паузы и синхронизирует сообщение-плеер."""
        self._player.resume()
        await interaction.response.send_message("Воспроизведение возобновлено.")
        await self._update_player_message(interaction)

    @app_commands.command(name="volume", description="Показать или установить громкость (0-200%)")
    @app_commands.describe(value="Громкость в процентах от 0 до 200")
    @app_commands.guild_only()
    async def volume(
        self,
        interaction: discord.Interaction,
        value: app_commands.Range[int, 0, 200] | None = None,
    ) -> None:
        """Без аргумента показывает текущую громкость, с аргументом — устанавливает новую."""
        if value is None:
            percent = round(self._player.volume * 100)
            await interaction.response.send_message(f"Текущая громкость: {percent}%.")
            return
        new_volume = self._player.set_volume(value / 100)
        percent = round(new_volume * 100)
        await interaction.response.send_message(f"Громкость установлена: {percent}%.")

    @app_commands.command(name="bass", description="Показать или установить уровень бас-буста")
    @app_commands.describe(level="Уровень бас-буста")
    @app_commands.choices(level=_BASS_CHOICES)
    @app_commands.guild_only()
    async def bass(
        self,
        interaction: discord.Interaction,
        level: app_commands.Choice[str] | None = None,
    ) -> None:
        """Без аргумента показывает текущий уровень бас-буста, с аргументом — устанавливает."""
        if level is None:
            current = self._player.bass.label
            await interaction.response.send_message(f"Текущий уровень бас-буста: {current}.")
            return
        await interaction.response.defer()
        new_level = BassLevel(level.value)
        await self._player.set_bass(new_level)
        await interaction.followup.send(f"Уровень бас-буста установлен: {new_level.label}.")

    @app_commands.command(name="nowplaying", description="Показать текущий трек")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        """Показывает исполнителя, название, волну, прогресс, громкость и уровень бас-буста."""
        info = self._player.now_playing()
        embed = discord.Embed(
            title="Сейчас играет", description=info.track.display, color=EMBED_COLOR
        )
        embed.add_field(name="Волна", value=self._player.wave_description or "—", inline=False)
        elapsed = format_duration(info.elapsed)
        total = format_duration(info.track.duration)
        embed.add_field(name="Прогресс", value=f"{elapsed} / {total}")
        embed.add_field(name="Громкость", value=f"{round(info.volume * 100)}%")
        embed.add_field(name="Бас-буст", value=info.bass.label)
        embed.add_field(name="Статус", value="на паузе" if info.paused else "играет")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="say", description="Произнести фразу голосом бота")
    @app_commands.describe(text="Что произнести")
    @app_commands.guild_only()
    async def say(
        self,
        interaction: discord.Interaction,
        # Ограничение длины прямо в форме Discord: человек упрётся в него
        # ещё при вводе, а не узнает об обрезке из ответа бота (сам предел
        # и его обоснование — в `bot.tts.MAX_TEXT_LENGTH`).
        text: app_commands.Range[str, 1, MAX_TEXT_LENGTH],
    ) -> None:
        """Озвучивает фразу в голосовом канале, приостановив музыку на её время."""
        await interaction.response.defer(ephemeral=True)
        await self._ensure_connected(interaction)
        spoken = await self.say_text(text)
        await interaction.followup.send(
            f"Произнесено: «{spoken}»",
            # text — свободный ввод пользователя: без явного none()
            # упоминание роли или @everyone внутри фразы ушло бы настоящим
            # пингом от имени бота (см. тот же приём в _search_and_start).
            allowed_mentions=discord.AllowedMentions.none(),
            ephemeral=True,
        )

    async def say_text(self, text: str) -> str:
        """Синтезирует фразу и произносит её; возвращает то, что было произнесено.

        Общая точка для текстовой команды `/say` и голосовой «Катя, повтори
        …» — чтобы обе вели себя одинаково и не разъехались при правках.
        Возвращается именно очищенный текст (схлопнутые пробелы, обрезка по
        длине, см. `bot.tts.clean_text`), а не исходный: человек должен
        видеть в подтверждении ровно то, что бот сказал вслух.
        """
        if not self._config.tts_enabled:
            raise SpeechSynthesisUnavailableError(
                "Синтез речи отключён настройкой TTS_ENABLED.",
                user_message="Синтез речи отключён на этом сервере.",
            )
        spoken = clean_text(text)
        if not spoken:
            raise NothingToSayError("Пустой текст для произнесения.")
        pcm = await self._tts.synthesize(spoken)
        if not pcm:
            raise SpeechSynthesisUnavailableError(
                f"Синтез вернул пустой звук для текста {spoken!r}",
                user_message="Не удалось произнести эту фразу.",
            )
        await self._player.say(pcm)
        return spoken

    @app_commands.command(name="queue", description="Показать следующий трек волны")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        """Показывает следующий забуференный трек волны."""
        tracks = self._player.queue_preview(1)
        if not tracks:
            await interaction.response.send_message(
                "Буфер пуст — «Моя волна» подберёт следующий трек на лету."
            )
            return
        embed = discord.Embed(
            title="Следующий трек", description=tracks[0].display, color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed)
