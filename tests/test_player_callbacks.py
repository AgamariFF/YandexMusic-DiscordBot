"""Тесты для колбэков плеера и обновления сообщения."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs.music import MusicCog
from bot.config import Config
from bot.player import GuildPlayer, PlayerState
from bot.yandex import TrackInfo


@pytest.fixture
def mock_config():
    """Мок конфигурации."""
    config = MagicMock(spec=Config)
    config.ffmpeg_path = "ffmpeg"
    config.default_volume = 0.5
    config.idle_timeout = 300
    config.guild_id = 12345
    return config


@pytest.fixture
def mock_client():
    """Мок Яндекс клиента."""
    return AsyncMock()


@pytest.fixture
def mock_bot():
    """Мок бота Discord."""
    bot = MagicMock(spec=discord.ext.commands.Bot)
    return bot


@pytest.fixture
def track_info():
    """Пример TrackInfo."""
    return TrackInfo(
        id="track_123",
        feedback_id="track_123:album_456",
        title="Test Track",
        artists="Test Artist",
        duration=180.0,
        cover_url="https://example.com/cover.jpg",
        raw=MagicMock(),
    )


class TestGuildPlayerStoppedCallback:
    """Тесты для колбэка on_stopped в GuildPlayer."""

    @pytest.mark.asyncio
    async def test_on_stopped_callback_called_on_real_stop(self, track_info):
        """on_stopped вызывается при реальной остановке волны."""
        callback = AsyncMock()
        player = GuildPlayer(
            AsyncMock(),
            on_stopped=callback,
        )

        # Запускаем волну (мокируя внутренние методы)
        player._current_track = track_info
        player._state = PlayerState.PLAYING

        # Вызываем _stop_locked_state
        await player._stop_locked_state()

        # Колбэк должен быть вызван
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_stopped_callback_not_called_when_already_stopped(self):
        """on_stopped не вызывается, если волна уже остановлена."""
        callback = AsyncMock()
        player = GuildPlayer(
            AsyncMock(),
            on_stopped=callback,
        )

        # Плеер изначально пуст (не был запущен)
        assert player._current_track is None
        assert player._session is None

        # Вызываем _stop_locked_state на пустом плеере
        await player._stop_locked_state()

        # Колбэк не должен быть вызван
        callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_stopped_callback_called_once_only(self, track_info):
        """on_stopped вызывается ровно один раз при остановке."""
        callback = AsyncMock()
        player = GuildPlayer(
            AsyncMock(),
            on_stopped=callback,
        )

        # Запускаем волну
        player._current_track = track_info
        player._state = PlayerState.PLAYING

        # Вызываем _stop_locked_state первый раз
        await player._stop_locked_state()

        # Вызываем второй раз на уже остановленном плеере
        await player._stop_locked_state()

        # Колбэк должен быть вызван только один раз (при первой остановке)
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_stopped_exception_does_not_break_stop(self, track_info):
        """Исключение в on_stopped не мешает остановке плеера."""
        callback = AsyncMock()
        callback.side_effect = RuntimeError("Callback error")

        player = GuildPlayer(
            AsyncMock(),
            on_stopped=callback,
        )

        # Запускаем волну
        player._current_track = track_info
        player._state = PlayerState.PLAYING

        # Вызываем _stop_locked_state — должно не упасть
        await player._stop_locked_state()

        # Состояние очищено несмотря на ошибку в колбэке
        assert player._current_track is None
        assert player._state == PlayerState.IDLE
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_stopped_called_when_session_exists(self):
        """on_stopped вызывается, если была session (даже без current_track)."""
        callback = AsyncMock()
        player = GuildPlayer(
            AsyncMock(),
            on_stopped=callback,
        )

        # Устанавливаем только session (без трека)
        player._session = MagicMock()
        player._current_track = None

        # Вызываем _stop_locked_state
        await player._stop_locked_state()

        # Колбэк должен быть вызван
        callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_stopped_none_is_allowed(self, track_info):
        """GuildPlayer работает с on_stopped=None."""
        player = GuildPlayer(
            AsyncMock(),
            on_stopped=None,
        )

        # Запускаем волну
        player._current_track = track_info

        # Вызываем _stop_locked_state — должно не упасть
        await player._stop_locked_state()

        assert player._current_track is None


class TestMusicCogUpdatePlayerMessage:
    """Тесты для MusicCog._update_player_message."""

    @pytest.mark.asyncio
    async def test_update_player_message_edits_existing_message(
        self, mock_bot, mock_config, mock_client
    ):
        """_update_player_message редактирует существующее сообщение."""
        from unittest.mock import patch

        with patch("bot.cogs.music.GuildPlayer"):
            cog = MusicCog(mock_bot, mock_config, mock_client)

        cog._announce_channel = MagicMock()

        # Устанавливаем существующее сообщение
        existing_message = AsyncMock(spec=discord.Message)
        existing_message.edit = AsyncMock()
        cog._player_message = existing_message

        # Мокируем плеер
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.current = None

        await cog._update_player_message()

        # Сообщение было отредактировано
        existing_message.edit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_player_message_sends_new_if_not_found(
        self, mock_bot, mock_config, mock_client
    ):
        """_update_player_message отправляет новое сообщение при discord.NotFound."""
        from unittest.mock import patch

        import discord as discord_module

        with patch("bot.cogs.music.GuildPlayer"):
            cog = MusicCog(mock_bot, mock_config, mock_client)

        # Канал для отправки сообщений
        announce_channel = AsyncMock()
        announce_channel.send = AsyncMock(spec=discord.Message)
        cog._announce_channel = announce_channel

        # Старое сообщение не найдено
        old_message = AsyncMock(spec=discord.Message)
        old_message.edit = AsyncMock(
            side_effect=discord_module.NotFound(MagicMock(), "Not found")
        )
        cog._player_message = old_message

        # Мокируем плеер
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.current = None

        await cog._update_player_message()

        # Новое сообщение было отправлено
        announce_channel.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_player_message_with_interaction_edits_response(
        self, mock_bot, mock_config, mock_client
    ):
        """_update_player_message использует interaction.response.edit_message."""
        from unittest.mock import patch

        with patch("bot.cogs.music.GuildPlayer"):
            cog = MusicCog(mock_bot, mock_config, mock_client)

        # Создаём mock взаимодействия
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)
        interaction.response.edit_message = AsyncMock()

        # Мокируем плеер
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.current = None

        await cog._update_player_message(interaction)

        # interaction.response.edit_message был вызван
        interaction.response.edit_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_player_message_stores_message(
        self, mock_bot, mock_config, mock_client
    ):
        """_update_player_message сохраняет ссылку на сообщение."""
        from unittest.mock import patch

        with patch("bot.cogs.music.GuildPlayer"):
            cog = MusicCog(mock_bot, mock_config, mock_client)

        # Канал для отправки
        announce_channel = AsyncMock()
        new_message = MagicMock(spec=discord.Message)
        announce_channel.send = AsyncMock(return_value=new_message)
        cog._announce_channel = announce_channel

        # Нет старого сообщения
        cog._player_message = None

        # Мокируем плеер
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.current = None

        await cog._update_player_message()

        # Новое сообщение сохранено
        assert cog._player_message is new_message

    @pytest.mark.asyncio
    async def test_announce_callback_updates_player_message(
        self, mock_bot, mock_config, mock_client, track_info
    ):
        """Колбэк _announce обновляет сообщение-плеер."""

        cog = MusicCog(mock_bot, mock_config, mock_client)

        # Мокируем _update_player_message
        cog._update_player_message = AsyncMock()

        # Вызываем _announce
        await cog._announce(track_info, "Моя волна")

        # _update_player_message был вызван
        cog._update_player_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_stopped_updates_player_message(
        self, mock_bot, mock_config, mock_client
    ):
        """Колбэк _handle_stopped обновляет сообщение-плеер."""
        cog = MusicCog(mock_bot, mock_config, mock_client)

        # Мокируем _update_player_message
        cog._update_player_message = AsyncMock()

        # Вызываем _handle_stopped
        await cog._handle_stopped()

        # _update_player_message был вызван
        cog._update_player_message.assert_called_once()
