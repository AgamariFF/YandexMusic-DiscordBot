"""Тесты для bot.cogs.player_view и связанных с ними функций."""

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from bot.cogs.music import build_player_embed
from bot.cogs.player_view import PlayerController, PlayerView
from bot.player import GuildPlayer, PlayerState
from bot.yandex import TrackInfo


class TestBuildPlayerEmbed:
    """Тесты для функции build_player_embed."""

    @pytest.fixture
    def track_info(self):
        """Пример TrackInfo для тестов."""
        return TrackInfo(
            id="track_123",
            feedback_id="track_123:album_456",
            title="Test Track",
            artists="Test Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=MagicMock(),
        )

    @pytest.fixture
    def color(self):
        """Цвет для embed'ов."""
        return discord.Color.gold()

    def test_build_player_embed_no_track_shows_stopped(self, color):
        """build_player_embed показывает 'остановлена' когда нет трека."""
        player = MagicMock(spec=GuildPlayer)
        player.current = None
        player.wave_description = None

        embed = build_player_embed(player, color)

        assert embed.title == "«Моя волна» остановлена"
        assert "завершено" in embed.description.lower()

    def test_build_player_embed_playing_shows_now_playing(self, track_info, color):
        """build_player_embed показывает 'Сейчас играет' когда плеер играет."""
        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PLAYING
        player.wave_description = "Моя волна"
        player.now_playing = MagicMock(
            return_value=MagicMock(elapsed=50.0)
        )

        embed = build_player_embed(player, color)

        # Проверяем author
        assert embed.author is not None
        assert embed.author.name == "Сейчас играет"

    def test_build_player_embed_paused_shows_paused(self, track_info, color):
        """build_player_embed показывает 'На паузе' когда плеер на паузе."""
        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PAUSED
        player.wave_description = "Моя волна"
        player.now_playing = MagicMock(
            return_value=MagicMock(elapsed=50.0)
        )

        embed = build_player_embed(player, color)

        # Проверяем author
        assert embed.author is not None
        assert embed.author.name == "На паузе"

    def test_build_player_embed_includes_cover_url(self, track_info, color):
        """build_player_embed вставляет обложку из cover_url трека."""
        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PLAYING
        player.wave_description = "Моя волна"
        player.now_playing = MagicMock(
            return_value=MagicMock(elapsed=50.0)
        )

        embed = build_player_embed(player, color)

        # Проверяем, что set_image был вызван с правильным URL
        assert embed.image is not None
        assert embed.image.url == "https://example.com/cover.jpg"

    def test_build_player_embed_no_cover_url_still_works(self, color):
        """build_player_embed работает без обложки (cover_url is None)."""
        track_info = TrackInfo(
            id="track_123",
            feedback_id="track_123:album_456",
            title="Test Track",
            artists="Test Artist",
            duration=180.0,
            cover_url=None,  # Нет обложки
            raw=MagicMock(),
        )
        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PLAYING
        player.wave_description = "Моя волна"
        player.now_playing = MagicMock(
            return_value=MagicMock(elapsed=50.0)
        )

        embed = build_player_embed(player, color)

        # Embed собирается без ошибок
        assert embed.title == "Моя волна"
        # Изображение не устанавливается
        assert embed.image is None or embed.image.url is None

    def test_build_player_embed_footer_includes_artist_and_title(
        self, track_info, color
    ):
        """build_player_embed включает исполнителя и название в footer."""
        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PLAYING
        player.wave_description = "Моя волна"
        player.now_playing = MagicMock(
            return_value=MagicMock(elapsed=50.0)
        )

        embed = build_player_embed(player, color)

        assert embed.footer is not None
        assert "Test Artist" in embed.footer.text
        assert "Test Track" in embed.footer.text

    def test_build_player_embed_footer_includes_duration(
        self, track_info, color
    ):
        """build_player_embed включает длительность в footer через „ • "."""
        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PLAYING
        player.wave_description = "Моя волна"
        player.now_playing = MagicMock(
            return_value=MagicMock(elapsed=90.0)
        )

        embed = build_player_embed(player, color)

        assert embed.footer is not None
        # Footer содержит длительность через „ • "
        assert "•" in embed.footer.text
        assert "3:00" in embed.footer.text

    def test_build_player_embed_handles_nothing_playing_error(
        self, track_info, color
    ):
        """build_player_embed возвращает 'остановлена' при NothingPlayingError.

        Гонка: трек был, но закончился между чтением current и now_playing.
        """
        from bot.errors import NothingPlayingError

        player = MagicMock(spec=GuildPlayer)
        player.current = track_info
        player.state = PlayerState.PLAYING
        player.wave_description = "Моя волна"
        player.now_playing.side_effect = NothingPlayingError()

        embed = build_player_embed(player, color)

        # Показываем "остановлена", а не падаем
        assert embed.title == "«Моя волна» остановлена"


class TestPlayerView:
    """Тесты для PlayerView."""

    @pytest.fixture
    def mock_player(self):
        """Мок плеера для тестов view."""
        player = MagicMock(spec=GuildPlayer)
        player.current = None
        player.state = PlayerState.IDLE
        player.channel = None
        return player

    @pytest.fixture
    def mock_controller(self):
        """Мок контроллера для тестов view."""
        controller = MagicMock(spec=PlayerController)
        return controller

    def test_player_view_init_with_no_track(self, mock_player, mock_controller):
        """PlayerView отключает кнопки при отсутствии трека."""
        mock_player.current = None

        view = PlayerView(player=mock_player, controller=mock_controller)

        # Проверяем, что view создан
        assert view is not None
        # Все кнопки кроме поиска должны быть отключены
        buttons = view.children
        pause_button = buttons[0]
        skip_button = buttons[1]
        search_button = buttons[2]
        disconnect_button = buttons[3]

        assert pause_button.disabled is True
        assert skip_button.disabled is True
        assert search_button.disabled is False
        assert disconnect_button.disabled is True

    def test_player_view_timeout_none(self, mock_player, mock_controller):
        """PlayerView имеет timeout=None."""
        view = PlayerView(player=mock_player, controller=mock_controller)
        assert view.timeout is None

    @pytest.mark.asyncio
    async def test_player_view_interaction_check_allows_same_channel(
        self, mock_player, mock_controller
    ):
        """interaction_check разрешает пользователя из того же канала."""
        voice_channel = MagicMock(spec=discord.VoiceChannel)
        voice_channel.id = 12345
        mock_player.channel = voice_channel

        view = PlayerView(player=mock_player, controller=mock_controller)

        # Создаём взаимодействие с пользователем в том же канале
        interaction = MagicMock(spec=discord.Interaction)
        member = MagicMock(spec=discord.Member)
        member.voice = MagicMock()
        member.voice.channel = voice_channel
        interaction.user = member

        result = await view.interaction_check(interaction)
        assert result is True

    @pytest.mark.asyncio
    async def test_player_view_interaction_check_denies_different_channel(
        self, mock_player, mock_controller
    ):
        """interaction_check запрещает пользователя из другого канала."""
        voice_channel = MagicMock(spec=discord.VoiceChannel)
        voice_channel.id = 12345
        mock_player.channel = voice_channel

        view = PlayerView(player=mock_player, controller=mock_controller)

        # Создаём взаимодействие с пользователем в другом канале
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        member = MagicMock(spec=discord.Member)
        other_channel = MagicMock(spec=discord.VoiceChannel)
        other_channel.id = 99999
        member.voice = MagicMock()
        member.voice.channel = other_channel
        interaction.user = member

        result = await view.interaction_check(interaction)
        assert result is False

    @pytest.mark.asyncio
    async def test_player_view_interaction_check_allows_all_when_bot_not_connected(
        self, mock_player, mock_controller
    ):
        """interaction_check разрешает всем при player.channel is None."""
        mock_player.channel = None

        view = PlayerView(player=mock_player, controller=mock_controller)

        # Любой пользователь должен быть разрешён
        interaction = MagicMock(spec=discord.Interaction)
        interaction.user = MagicMock()  # Любой пользователь

        result = await view.interaction_check(interaction)
        assert result is True

    @pytest.mark.asyncio
    async def test_player_view_interaction_check_denies_non_member(
        self, mock_player, mock_controller
    ):
        """interaction_check запрещает не-Member."""
        voice_channel = MagicMock(spec=discord.VoiceChannel)
        voice_channel.id = 12345
        mock_player.channel = voice_channel

        view = PlayerView(player=mock_player, controller=mock_controller)

        # Создаём взаимодействие с обычным User (не Member)
        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.user = MagicMock(spec=discord.User)  # Не Member

        result = await view.interaction_check(interaction)
        assert result is False
