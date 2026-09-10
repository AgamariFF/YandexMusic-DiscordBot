"""Ког со slash-командами голосовой чат-рулетки (nekto.me).

Взаимоисключение с «Моей волной» реализовано только в одну сторону: запуск
рулетки сначала останавливает `GuildPlayer` музыкального кога (см.
`_ensure_music_stopped`), потому что у бота одно голосовое соединение на
гильдию и Discord не даст открыть второе, пока активно первое. Обратной
защиты (запуск волны поверх уже идущей рулетки) здесь нет — `bot/cogs/music.py`
сейчас недоступен для правки (см. задачу), и сама волна не умеет проверять
чужое состояние. Это временное ограничение, которое нужно закрыть отдельно,
когда `music.py` снова станет доступен для изменений.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.cogs.roulette_view import RouletteView
from bot.config import Config
from bot.errors import BotError, NotInVoiceChannelError
from bot.player import GuildPlayer
from bot.roulette import GuildRoulette, RouletteStatus

logger = logging.getLogger(__name__)

EMBED_COLOR = discord.Color.purple()


def build_roulette_embed(roulette: GuildRoulette, color: discord.Color) -> discord.Embed:
    """Собирает embed единственного сообщения-статуса по текущему состоянию `GuildRoulette`.

    Единственное место, где строится вид статуса — и колбэк `_update_status_message`
    (срабатывающий на события `GuildRoulette`), и обе кнопки `RouletteView`
    перерисовывают сообщение через эту же функцию, поэтому текст не может
    разойтись сам с собой (тот же приём, что и у `build_player_embed`).
    """
    status = roulette.status
    if status is RouletteStatus.SEARCHING:
        return discord.Embed(
            title="Чат-рулетка: идёт поиск собеседника…",
            description="Как только кто-то найдётся, участники канала услышат его голос.",
            color=color,
        )
    if status is RouletteStatus.FOUND:
        return discord.Embed(
            title="Чат-рулетка: собеседник найден",
            description="Говорите — вас уже слышно.",
            color=color,
        )
    if status is RouletteStatus.LEFT:
        return discord.Embed(
            title="Чат-рулетка: собеседник покинул разговор",
            description="Ищем нового собеседника…",
            color=color,
        )
    embed = discord.Embed(title="Чат-рулетка остановлена", color=color)
    embed.description = roulette.stop_reason or "Бот отключился от голосового канала."
    return embed


def _voice_channel_of(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel:
    """Возвращает голосовой канал участника, вызвавшего команду.

    Дублирует одноимённый приватный хелпер `bot.cogs.music` — импортировать
    оттуда нечего: функция там не публична, а сам файл нельзя менять, чтобы
    её экспортировать (см. модульный докстринг про ограничения на правку
    `music.py`). Реализация тривиальна, дублирование сознательное.
    """
    user = interaction.user
    if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
        raise NotInVoiceChannelError()
    return user.voice.channel


class RouletteCog(commands.Cog, name="Чат-рулетка"):
    """Slash-команды голосовой чат-рулетки: старт, следующий собеседник, стоп."""

    def __init__(self, bot: commands.Bot, config: Config, player: GuildPlayer) -> None:
        """Создаёт единственный `GuildRoulette`, обслуживающий сервер бота.

        `player` — музыкальный `GuildPlayer` этого же сервера, нужен только
        чтобы останавливать волну перед запуском рулетки (см. докстринг
        модуля про взаимоисключение); сам ког музыки сюда не передаётся.
        """
        self._bot = bot
        self._config = config
        self._player = player
        self._announce_channel: discord.abc.Messageable | None = None
        # Единственное сообщение-статус сервера, его текущий RouletteView и
        # лок, под которым идёт любая правка — см. докстринг
        # `_update_status_message`, устроено по образцу
        # `MusicCog._player_message`/`_player_view`/`_player_message_lock`.
        self._status_message: discord.Message | discord.InteractionMessage | None = None
        self._status_view: RouletteView | None = None
        self._status_message_lock = asyncio.Lock()
        self._roulette = GuildRoulette(
            token=config.nekto_token,
            user_agent=config.nekto_user_agent,
            on_status_changed=self._update_status_message,
        )

    @property
    def roulette(self) -> GuildRoulette:
        """Единственная чат-рулетка, обслуживающая сервер бота.

        Нужна другим когам, которым приходится делить с рулеткой одно
        голосовое соединение гильдии — сейчас это `bot.cogs.listen.ListenCog`
        (см. её докстринг про взаимоисключение), по тому же образцу, что и
        `MusicCog.player`.
        """
        return self._roulette

    async def cog_unload(self) -> None:
        """Останавливает текущий RouletteView и саму рулетку при выгрузке кога."""
        if self._status_view is not None:
            self._status_view.stop()
        await self._roulette.stop()

    async def _update_status_message(self, interaction: discord.Interaction | None = None) -> None:
        """Обновляет сообщение-статус на месте, либо отправляет новое взамен утраченного.

        Проще, чем `MusicCog._update_player_message`: у чат-рулетки нет
        событий вроде смены трека, ради которых сообщение стоит поднимать
        из-под свежей переписки — оно всегда правится на месте, а новое
        отправляется только если прежнего не осталось (ни разу не отправляли
        либо его удалили вручную). Логика "редактировать через interaction,
        иначе через сохранённое сообщение, иначе отправить новое" всё равно
        совпадает с `_update_player_message` — под тем же локом, по тем же
        причинам (колбэк `on_status_changed` и нажатия кнопок могут
        сработать почти одновременно).
        """
        async with self._status_message_lock:
            embed = build_roulette_embed(self._roulette, EMBED_COLOR)
            view = RouletteView(roulette=self._roulette, controller=self)
            mentions = discord.AllowedMentions.none()

            if interaction is not None and not interaction.response.is_done():
                try:
                    await interaction.response.edit_message(
                        embed=embed, view=view, allowed_mentions=mentions
                    )
                    self._replace_status_view(view, message=interaction.message)
                    return
                except discord.NotFound:
                    pass

            if (interaction is None or interaction.response.is_done()) and (
                self._status_message is not None
            ):
                try:
                    await self._status_message.edit(
                        embed=embed, view=view, allowed_mentions=mentions
                    )
                    self._replace_status_view(view, message=self._status_message)
                    return
                except discord.NotFound:
                    self._status_message = None

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
                self._replace_status_view(view, message=message)
                return

            if self._announce_channel is None:
                return
            message = await self._announce_channel.send(
                embed=embed, view=view, allowed_mentions=mentions
            )
            self._replace_status_view(view, message=message)

    def _replace_status_view(
        self, view: RouletteView, *, message: discord.Message | discord.InteractionMessage
    ) -> None:
        """Запоминает новые сообщение и view, останавливая предыдущий view.

        См. докстринг `MusicCog._replace_player_view` — та же причина:
        `View.stop()` снимает записи старого экземпляра из `ViewStore`
        discord.py, иначе они копились бы там на всё время жизни рулетки.
        """
        old_view = self._status_view
        self._status_message = message
        self._status_view = view
        if old_view is not None:
            old_view.stop()

    async def _ensure_music_stopped(self) -> None:
        """Останавливает «Мою волну», если она играла — оба кога делят одно голосовое соединение.

        `GuildPlayer.disconnect()` идемпотентен, так что вызывать его
        безопасно и тогда, когда волна и не играла.
        """
        await self._player.disconnect()

    async def _send_started_ack(self, interaction: discord.Interaction) -> None:
        """Подтверждает запуск рулетки, правя исходный ответ — см. `MusicCog._send_started_ack`.

        Публичное сообщение-статус к этому моменту уже отправил колбэк
        `_update_status_message` (сработал изнутри `GuildRoulette.start()`
        при переходе в состояние "идёт поиск"), а на само взаимодействие
        всё равно нужно ответить.
        """
        text = "Чат-рулетка запущена — смотрите сообщение-статус в канале."
        if interaction.response.is_done():
            try:
                await interaction.edit_original_response(content=text, embed=None, view=None)
            except discord.HTTPException:  # NotFound — её подкласс
                await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    async def handle_next(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки «Следующий» на сообщении-статусе."""
        await interaction.response.defer()
        await self._roulette.next_peer()
        await self._update_status_message(interaction)

    async def handle_stop(self, interaction: discord.Interaction) -> None:
        """Обрабатывает нажатие кнопки «Стоп» на сообщении-статусе."""
        await interaction.response.defer()
        await self._roulette.stop()
        await self._update_status_message(interaction)

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

    @app_commands.command(name="roulette", description="Начать голосовую чат-рулетку")
    @app_commands.guild_only()
    async def roulette_start(self, interaction: discord.Interaction) -> None:
        """Останавливает волну, подключается к каналу вызвавшего и начинает поиск собеседника."""
        await interaction.response.defer(ephemeral=True)
        channel = _voice_channel_of(interaction)
        await self._ensure_music_stopped()
        self._announce_channel = interaction.channel
        await self._roulette.start(channel)
        await self._send_started_ack(interaction)

    @app_commands.command(
        name="roulette_next", description="Перейти к следующему собеседнику чат-рулетки"
    )
    @app_commands.guild_only()
    async def roulette_next(self, interaction: discord.Interaction) -> None:
        """Завершает разговор с текущим собеседником и ищет нового."""
        await interaction.response.defer()
        await self._roulette.next_peer()
        await interaction.followup.send("Ищу нового собеседника.")
        await self._update_status_message(interaction)

    @app_commands.command(
        name="roulette_stop", description="Завершить чат-рулетку и отключиться от канала"
    )
    @app_commands.guild_only()
    async def roulette_stop(self, interaction: discord.Interaction) -> None:
        """Останавливает чат-рулетку и отключает бота от голосового канала."""
        await interaction.response.defer()
        await self._roulette.stop()
        await interaction.followup.send("Чат-рулетка остановлена, бот отключился от канала.")
        await self._update_status_message(interaction)
