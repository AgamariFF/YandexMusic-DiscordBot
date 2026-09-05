"""Ког со slash-командами управления «Моей волной» Яндекс.Музыки."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.audio.bassboost import BassLevel
from bot.config import Config
from bot.errors import BotError, NotInVoiceChannelError
from bot.player import GuildPlayer
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
        self._announce_channel: discord.abc.Messageable | None = None
        self._player = GuildPlayer(
            client,
            ffmpeg_path=config.ffmpeg_path,
            default_volume=config.default_volume,
            idle_timeout=config.idle_timeout,
            announce=self._announce,
        )

    @property
    def player(self) -> GuildPlayer:
        """Единственный проигрыватель волны, обслуживающий сервер бота."""
        return self._player

    async def cog_unload(self) -> None:
        """Корректно отключает плеер от голосового канала при выгрузке кога."""
        await self._player.disconnect()

    async def _announce(self, track: TrackInfo) -> None:
        """Отправляет анонс нового трека в последний канал запуска волны, если он известен."""
        if self._announce_channel is None:
            return
        embed = discord.Embed(title="Сейчас играет", description=track.display, color=EMBED_COLOR)
        await self._announce_channel.send(embed=embed)

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
            except (discord.HTTPException, discord.NotFound):
                await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

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
        await interaction.response.defer()
        if self._player.voice_client is None:
            channel = _voice_channel_of(interaction)
            await self._player.connect(channel)
        self._announce_channel = interaction.channel
        track = await self._player.start_wave()
        embed = discord.Embed(
            title="«Моя волна» запущена", description=track.display, color=EMBED_COLOR
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="skip", description="Пропустить текущий трек")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        """Пропускает текущий воспроизводимый трек."""
        await interaction.response.defer()
        next_track = await self._player.skip()
        if next_track is None:
            await interaction.followup.send("Трек пропущен.")
        else:
            await interaction.followup.send(f"Трек пропущен. Далее: {next_track.display}")

    @app_commands.command(name="pause", description="Поставить воспроизведение на паузу")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        """Ставит текущее воспроизведение на паузу."""
        self._player.pause()
        await interaction.response.send_message("Воспроизведение приостановлено.")

    @app_commands.command(name="resume", description="Возобновить воспроизведение")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        """Возобновляет воспроизведение после паузы."""
        self._player.resume()
        await interaction.response.send_message("Воспроизведение возобновлено.")

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
        """Показывает исполнителя, название, прогресс, громкость и уровень бас-буста."""
        info = self._player.now_playing()
        embed = discord.Embed(
            title="Сейчас играет", description=info.track.display, color=EMBED_COLOR
        )
        elapsed = format_duration(info.elapsed)
        total = format_duration(info.track.duration)
        embed.add_field(name="Прогресс", value=f"{elapsed} / {total}")
        embed.add_field(name="Громкость", value=f"{round(info.volume * 100)}%")
        embed.add_field(name="Бас-буст", value=info.bass.label)
        embed.add_field(name="Статус", value="на паузе" if info.paused else "играет")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="queue", description="Показать ближайшие треки волны")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        """Показывает до 10 ближайших треков из буфера волны."""
        tracks = self._player.queue_preview(10)
        if not tracks:
            await interaction.response.send_message(
                "Буфер пуст — «Моя волна» подберёт треки на лету."
            )
            return
        lines = [f"{i}. {track.display}" for i, track in enumerate(tracks, start=1)]
        embed = discord.Embed(
            title="Ближайшие треки", description="\n".join(lines), color=EMBED_COLOR
        )
        await interaction.response.send_message(embed=embed)
