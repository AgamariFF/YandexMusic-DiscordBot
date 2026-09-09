"""Ког со slash-командами управления «Моей волной» Яндекс.Музыки."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.audio.bassboost import BassLevel
from bot.cogs.player_view import PlayerView
from bot.cogs.views import MAX_SEARCH_RESULTS, TrackSearchView, build_search_embed
from bot.config import Config
from bot.errors import BotError, NothingPlayingError, NotInVoiceChannelError
from bot.player import GuildPlayer, PlayerState
from bot.yandex import TrackInfo, YandexMusicClient

logger = logging.getLogger(__name__)

EMBED_COLOR = discord.Color.gold()

_BASS_CHOICES = [app_commands.Choice(name=level.label, value=level.value) for level in BassLevel]

#: Ширина полосы прогресса в символах (без учёта текста времени рядом).
_PROGRESS_BAR_WIDTH = 10
_PROGRESS_BAR_FILLED = "▰"
_PROGRESS_BAR_EMPTY = "▱"


def format_duration(seconds: float) -> str:
    """Форматирует длительность как M:SS, либо H:MM:SS для длительностей от часа."""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def render_progress_bar(elapsed: float, duration: float) -> str | None:
    """Строит текстовую полосу прогресса вида «▰▰▰▰▱▱▱▱▱▱ 1:23 / 3:55».

    Возвращает `None`, если длительность неизвестна или нулевая (бывает у
    отдельных треков волны) — рисовать полосу тогда нечем, вызывающий код
    должен в этом случае показать только название трека.
    """
    if duration <= 0:
        return None
    clamped_elapsed = max(0.0, min(elapsed, duration))
    filled = round(_PROGRESS_BAR_WIDTH * clamped_elapsed / duration)
    filled = max(0, min(_PROGRESS_BAR_WIDTH, filled))
    bar = _PROGRESS_BAR_FILLED * filled + _PROGRESS_BAR_EMPTY * (_PROGRESS_BAR_WIDTH - filled)
    return f"{bar} {format_duration(clamped_elapsed)} / {format_duration(duration)}"


def build_player_embed(player: GuildPlayer, color: discord.Color) -> discord.Embed:
    """Собирает embed единственного сообщения-плеера по текущему состоянию `GuildPlayer`.

    Единственное место, где строится вид плеера: и колбэки `_announce`/
    `_handle_stopped`, и каждая кнопка `PlayerView` перерисовывают сообщение
    через эту же функцию, поэтому оно не может разойтись само с собой.

    Порядок в embed'е сверху вниз — author → title → большая картинка →
    footer. Обложка ставится большой картинкой (`set_image`), а Discord не
    даёт разместить произвольный текст НИЖЕ такой картинки — только footer.
    Раз по ТЗ именно там (под обложкой) должны быть исполнитель, название и
    прогресс, footer — единственное подходящее место; это осознанный
    компромисс структуры embed'а, а не недосмотр.
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

    bar = render_progress_bar(info.elapsed, track.duration)
    footer_text = f"{track.artists} — {track.title}"
    if bar is not None:
        footer_text = f"{footer_text}\n{bar}"
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

    def __init__(self, bot: commands.Bot, config: Config, client: YandexMusicClient) -> None:
        """Создаёт единственный GuildPlayer, обслуживающий сервер бота."""
        self._bot = bot
        self._config = config
        self._client = client
        self._announce_channel: discord.abc.Messageable | None = None
        # Единственное сообщение-плеер сервера и лок, под которым идёт любая
        # его правка — подробности см. в докстринге `_update_player_message`.
        self._player_message: discord.Message | discord.InteractionMessage | None = None
        self._player_message_lock = asyncio.Lock()
        self._player = GuildPlayer(
            client,
            ffmpeg_path=config.ffmpeg_path,
            default_volume=config.default_volume,
            idle_timeout=config.idle_timeout,
            announce=self._announce,
            on_stopped=self._handle_stopped,
        )

    @property
    def player(self) -> GuildPlayer:
        """Единственный проигрыватель волны, обслуживающий сервер бота."""
        return self._player

    async def cog_unload(self) -> None:
        """Корректно отключает плеер от голосового канала при выгрузке кога."""
        await self._player.disconnect()

    async def _announce(self, track: TrackInfo, wave_description: str | None) -> None:
        """Отражает старт нового трека в сообщении-плеере.

        `track` и `wave_description` не читаются напрямую: `_update_player_message`
        берёт актуальное состояние `self._player` в момент вызова. Расхождения
        не будет — `_play_track_locked` обновляет состояние плеера ДО вызова
        этого колбэка (см. докстринг `GuildPlayer`).
        """
        await self._update_player_message()

    async def _handle_stopped(self) -> None:
        """Отражает остановку волны в сообщении-плеере — колбэк `GuildPlayer.on_stopped`.

        Срабатывает событийно (см. `GuildPlayer._stop_locked_state`), а не по
        опросу: `on_stopped` гарантирует однократность и молчит, если волна и
        не запускалась, поэтому здесь достаточно просто перерисовать сообщение.
        """
        await self._update_player_message()

    async def _update_player_message(self, interaction: discord.Interaction | None = None) -> None:
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
                    return
                except discord.NotFound:
                    # Сообщение с кнопкой удалили вручную; response ещё не
                    # израсходован (edit_message не дошёл до сети) — упадём в
                    # отправку нового ниже, ответив тем же взаимодействием.
                    pass

            if (interaction is None or interaction.response.is_done()) and (
                self._player_message is not None
            ):
                try:
                    await self._player_message.edit(
                        embed=embed, view=view, allowed_mentions=mentions
                    )
                    return
                except discord.NotFound:
                    self._player_message = None

            if interaction is not None:
                if interaction.response.is_done():
                    self._player_message = await interaction.followup.send(
                        embed=embed, view=view, allowed_mentions=mentions, wait=True
                    )
                else:
                    await interaction.response.send_message(
                        embed=embed, view=view, allowed_mentions=mentions
                    )
                    self._player_message = await interaction.original_response()
                return

            if self._announce_channel is None:
                return
            self._player_message = await self._announce_channel.send(
                embed=embed, view=view, allowed_mentions=mentions
            )

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
        """Короткое эфемерное подтверждение запуска волны.

        Раньше команды слали публичный embed «Моя волна запущена» отдельным
        сообщением на каждый запуск, а `_announce` — ещё один embed на
        каждый трек: получался спам параллельно с сообщением-плеером. Теперь
        сам плеер — единственное публичное сообщение (его к этому моменту
        уже обновил колбэк `_announce`), а на взаимодействие всё равно нужно
        ответить, иначе Discord покажет пользователю ошибку, — поэтому здесь
        только короткое эфемерное "ок".
        """
        text = "«Моя волна» запущена — смотрите сообщение-плеер в канале."
        if interaction.response.is_done():
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

    async def _search_and_start(self, interaction: discord.Interaction, query: str) -> None:
        """Ищет треки и либо сразу запускает волну, либо показывает меню выбора.

        Общая ветка (пусто/один/несколько) для команды /search и модального
        окна поиска на кнопке плеера — чтобы не дублировать её в двух местах.
        Требует, чтобы `interaction.response` был уже отложен (`defer`)
        вызывающим кодом.
        """
        tracks = await self._client.search_tracks(query, limit=MAX_SEARCH_RESULTS)

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
