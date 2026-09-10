"""Ког со slash-командами голосового распознавания речи (офлайн, через Vosk).

Прослушивание не может делить голосовое соединение гильдии ни с «Моей
волной» (`bot.player.GuildPlayer`), ни с чат-рулеткой
(`bot.roulette.GuildRoulette`) — Discord не даёт открыть второе голосовое
соединение гильдии, пока активно первое (та же причина, по которой
`RouletteCog` останавливает волну перед запуском рулетки). Взаимоисключение
реализовано только в одну сторону, тем же приёмом, что и у `RouletteCog`
(см. его модульный докстринг): запуск прослушивания сначала принудительно
останавливает и волну, и чат-рулетку (`_ensure_voice_free`). Обратной
защиты нет — запуск `/wave` или `/roulette` поверх уже идущего
прослушивания просто упадёт с понятной ошибкой подключения
(`VoiceConnectError`), потому что Discord не даёт открыть второе голосовое
соединение гильдии; это то же самое временное ограничение, что уже описано
у `RouletteCog`, только теперь на троих участников одного и того же
ограниченного ресурса.

**Важно про приватность**: пока прослушивание запущено, бот распознаёт и
публикует в текстовый канал речь всех участников голосового канала, к
которому подключён — это явная и заметная активность (см. README, раздел
про распознавание речи), а не скрытая.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot.config import Config
from bot.errors import BotError, NotInVoiceChannelError
from bot.listening import GuildListener
from bot.player import GuildPlayer
from bot.roulette import GuildRoulette
from bot.speech import SpeechRecognizer

logger = logging.getLogger(__name__)


def _voice_channel_of(
    interaction: discord.Interaction,
) -> discord.VoiceChannel | discord.StageChannel:
    """Возвращает голосовой канал участника, вызвавшего команду.

    Дублирует одноимённые приватные хелперы `bot.cogs.music`/`bot.cogs.roulette`
    — по тем же причинам, что и у `RouletteCog` (см. её докстринг):
    реализация тривиальна, дублирование сознательное.
    """
    user = interaction.user
    if not isinstance(user, discord.Member) or user.voice is None or user.voice.channel is None:
        raise NotInVoiceChannelError()
    return user.voice.channel


class ListenCog(commands.Cog, name="Распознавание речи"):
    """Slash-команды голосового распознавания речи: начать и остановить прослушивание."""

    def __init__(
        self,
        bot: commands.Bot,
        config: Config,
        player: GuildPlayer,
        roulette: GuildRoulette | None,
    ) -> None:
        """Создаёт единственные `SpeechRecognizer` и `GuildListener`, обслуживающие сервер бота.

        `player`/`roulette` нужны только чтобы освобождать голосовое
        соединение перед запуском прослушивания (см. модульный докстринг
        про взаимоисключение); `roulette` может быть `None`, если чат-рулетка
        отключена конфигурацией (нет `NEKTO_TOKEN`, см. `bot/__main__.py`) —
        тогда её просто нечего останавливать.
        """
        self._bot = bot
        self._config = config
        self._player = player
        self._roulette = roulette
        self._text_channel: discord.abc.Messageable | None = None
        self._recognizer = SpeechRecognizer(model_path=config.speech_model_path)
        self._listener = GuildListener(recognizer=self._recognizer, on_phrase=self._handle_phrase)

    async def cog_unload(self) -> None:
        """Останавливает прослушивание и освобождает модель распознавания при выгрузке кога."""
        await self._listener.stop()
        await self._recognizer.close()

    async def _handle_phrase(self, speaker_id: int, text: str) -> None:
        """Пишет распознанную фразу в текстовый канал в виде «кто сказал — что сказал».

        Это единственная проверка того, что распознавание вообще работает
        (см. задачу) — команд, реагирующих на смысл фразы, здесь сознательно
        нет, это фундамент под них.
        """
        if self._text_channel is None:
            return
        channel = self._listener.channel
        member = channel.guild.get_member(speaker_id) if channel is not None else None
        name = member.display_name if member is not None else f"пользователь {speaker_id}"
        await self._text_channel.send(
            f"**{name}:** {text}",
            # text — распознанная речь, свободный ввод чужого человека, а
            # не самого бота; без явного none() упоминание роли или
            # @everyone внутри фразы ушло бы как настоящий пинг от имени
            # бота (см. тот же комментарий в bot.cogs.music._search_and_start
            # — в проекте уже была такая уязвимость).
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _ensure_voice_free(self) -> None:
        """Останавливает волну и чат-рулетку, если они были активны — все три делят одно соединение.

        `GuildPlayer.disconnect()` и `GuildRoulette.stop()` идемпотентны,
        так что вызывать их безопасно и тогда, когда ничего из этого не
        было запущено.
        """
        await self._player.disconnect()
        if self._roulette is not None:
            await self._roulette.stop()

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

    @app_commands.command(name="listen", description="Начать распознавание речи в голосовом канале")
    @app_commands.guild_only()
    async def listen_start(self, interaction: discord.Interaction) -> None:
        """Останавливает волну и рулетку, подключается к каналу вызвавшего и начинает слушать."""
        await interaction.response.defer(ephemeral=True)
        channel = _voice_channel_of(interaction)
        await self._ensure_voice_free()
        self._text_channel = interaction.channel
        await self._listener.start(channel)
        await interaction.followup.send(
            f"Слушаю канал «{channel.name}» — распознанные фразы буду писать сюда.",
            ephemeral=True,
        )

    @app_commands.command(
        name="listen_stop", description="Остановить распознавание речи и отключиться от канала"
    )
    @app_commands.guild_only()
    async def listen_stop(self, interaction: discord.Interaction) -> None:
        """Останавливает распознавание речи и отключает бота от голосового канала."""
        await interaction.response.defer()
        await self._listener.stop()
        await interaction.followup.send("Распознавание речи остановлено, бот отключился от канала.")
