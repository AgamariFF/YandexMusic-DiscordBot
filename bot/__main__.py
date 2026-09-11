"""Точка входа Discord-бота «Моя волна»."""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import discord
from discord.ext import commands

from bot.cogs.music import MusicCog
from bot.cogs.roulette import RouletteCog
from bot.cogs.voice_control import VoiceControlCog
from bot.config import Config, load_config
from bot.errors import ConfigError, YandexAuthError
from bot.logging_setup import setup_logging
from bot.voice_dave import diagnostics_enabled
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

        # VoiceControlCog создаётся раньше MusicCog и без него: его хуки
        # (`on_player_voice_connected`/`on_player_voice_disconnected`) нужно
        # передать в конструктор GuildPlayer уже при создании MusicCog, а
        # сам MusicCog для их вызова (execute_voice_command) он получит чуть
        # позже, через bind_music_cog — см. докстринг VoiceControlCog.
        voice_control_cog: VoiceControlCog | None = None
        if self._config.speech_enabled:
            voice_control_cog = VoiceControlCog(self, self._config)

        music_cog = MusicCog(
            self,
            self._config,
            client,
            on_voice_connected=(
                voice_control_cog.on_player_voice_connected if voice_control_cog else None
            ),
            on_voice_disconnected=(
                voice_control_cog.on_player_voice_disconnected if voice_control_cog else None
            ),
        )
        await self.add_cog(music_cog)

        if voice_control_cog is not None:
            voice_control_cog.bind_music_cog(music_cog)
            await self.add_cog(voice_control_cog)
            # Path.is_dir() — блокирующий вызов файловой системы; сам по себе
            # он почти мгновенный (это не загрузка модели, только проверка
            # существования каталога), но в корутине event loop даже такой
            # быстрый блокирующий вызов лучше не делать напрямую — уносим в поток.
            speech_model_found = await asyncio.to_thread(
                Path(self._config.speech_model_path).is_dir
            )
            if speech_model_found:
                logger.info(
                    "Распознавание речи и голосовые команды включены (SPEECH_ENABLED), модель: %s",
                    self._config.speech_model_path,
                )
            else:
                logger.warning(
                    "Модель распознавания речи не найдена по пути %s — голосовое управление "
                    "отключится при первом же подключении к голосовому каналу, пока модель не "
                    "будет скачана (см. README).",
                    self._config.speech_model_path,
                )
        else:
            logger.info(
                "SPEECH_ENABLED=0 — распознавание речи и голосовые команды отключены."
            )

        if self._config.nekto_token:
            roulette_cog = RouletteCog(self, self._config, music_cog.player)
            await self.add_cog(roulette_cog)
            logger.info("Чат-рулетка включена.")
        else:
            logger.info(
                "NEKTO_TOKEN не задан — команды чат-рулетки отключены, "
                "остальной функционал бота не затронут."
            )

        if diagnostics_enabled():
            logger.info(
                "Подробная диагностика приёма голоса включена (VOICE_RECV_DIAG) — "
                "лог первых пакетов и далее раз в N, см. bot.voice_dave."
            )

        guild = discord.Object(id=self._config.guild_id)
        self.tree.copy_global_to(guild=guild)
        try:
            async with asyncio.timeout(15):
                await self.tree.sync(guild=guild)
        except TimeoutError:
            logger.error(
                "Синхронизация команд для сервера %s превысила 15 секунд; продолжаем запуск бота.",
                self._config.guild_id,
            )
        except discord.HTTPException:
            logger.exception(
                "Не удалось синхронизировать команды для сервера %s; продолжаем запуск бота.",
                self._config.guild_id,
            )
        else:
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
