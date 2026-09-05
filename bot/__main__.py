"""Точка входа Discord-бота «Моя волна»."""

from __future__ import annotations

import logging
import sys

import discord
from discord.ext import commands

from bot.cogs.music import MusicCog
from bot.config import Config, load_config
from bot.errors import ConfigError, YandexAuthError
from bot.logging_setup import setup_logging
from bot.yandex import YandexMusicClient

logger = logging.getLogger(__name__)


class WaveBot(commands.Bot):
    """Discord-бот, обслуживающий «Мою волну» на одном сервере."""

    def __init__(self, config: Config) -> None:
        """Настраивает интенты бота и запоминает конфигурацию."""
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self._config = config
        self._yandex_client: YandexMusicClient | None = None
        self.fatal_error = False

    async def setup_hook(self) -> None:
        """Подключает Яндекс-клиент, регистрирует ког и синхронизирует команды сервера."""
        client = YandexMusicClient(self._config.yandex_token)
        try:
            await client.connect()
        except YandexAuthError:
            logger.error("Не удалось авторизоваться в Яндекс.Музыке: неверный токен.")
            await client.close()
            self.fatal_error = True
            await self.close()
            return
        self._yandex_client = client

        await self.add_cog(MusicCog(self, self._config, client))

        guild = discord.Object(id=self._config.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        logger.info("Команды синхронизированы для сервера %s", self._config.guild_id)

    async def on_ready(self) -> None:
        """Логирует успешное подключение бота к Discord."""
        logger.info("Бот %s готов, гильдия %s", self.user, self._config.guild_id)

    async def close(self) -> None:
        """Отключает голосовые соединения и закрывает Яндекс-клиент перед выходом."""
        for voice_client in list(self.voice_clients):
            try:
                await voice_client.disconnect(force=True)
            except Exception:
                logger.warning("Ошибка при отключении голосового клиента", exc_info=True)
        if self._yandex_client is not None:
            await self._yandex_client.close()
        await super().close()


def main() -> None:
    """Загружает конфигурацию, настраивает логирование и запускает бота."""
    try:
        config = load_config()
    except ConfigError as exc:
        logging.basicConfig(level="INFO")
        logging.getLogger(__name__).error("%s", exc)
        sys.exit(1)

    setup_logging(config.log_level, secrets=config.secrets)

    bot = WaveBot(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        logger.error("Discord-токен невалиден.")
        sys.exit(1)
    except discord.PrivilegedIntentsRequired:
        logger.error(
            "Боту не хватает привилегированных интентов. Включите их в Discord Developer Portal."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C.")
        return

    if bot.fatal_error:
        sys.exit(1)


if __name__ == "__main__":
    main()
