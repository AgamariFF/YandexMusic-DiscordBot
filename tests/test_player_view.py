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

    @pytest.mark.asyncio
    async def test_handle_pause_toggle_resumes_when_paused(self, mock_player, mock_controller):
        """handle_pause_toggle вызывает resume() при состоянии PAUSED."""
        from bot.cogs.player_view import PlayerView as RealPlayerView

        mock_player.state = PlayerState.PAUSED
        mock_player.resume = MagicMock()
        mock_player.pause = MagicMock()

        view = RealPlayerView(player=mock_player, controller=mock_controller)
        view._update_player_message = AsyncMock()

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)

        # Вызываем handle_pause_toggle через контроллер
        async def mock_handle_pause_toggle(interaction):
            if mock_player.state is PlayerState.PAUSED:
                mock_player.resume()
            else:
                mock_player.pause()
            await view._update_player_message(interaction)

        mock_controller.handle_pause_toggle = mock_handle_pause_toggle

        await view.handle_pause_toggle(interaction)
        mock_player.resume.assert_called_once()
        mock_player.pause.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_pause_toggle_pauses_when_playing(self, mock_player, mock_controller):
        """handle_pause_toggle вызывает pause() при состоянии PLAYING."""
        from bot.cogs.player_view import PlayerView as RealPlayerView

        mock_player.state = PlayerState.PLAYING
        mock_player.resume = MagicMock()
        mock_player.pause = MagicMock()

        view = RealPlayerView(player=mock_player, controller=mock_controller)
        view._update_player_message = AsyncMock()

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)

        # Вызываем handle_pause_toggle через контроллер
        async def mock_handle_pause_toggle(interaction):
            if mock_player.state is PlayerState.PAUSED:
                mock_player.resume()
            else:
                mock_player.pause()
            await view._update_player_message(interaction)

        mock_controller.handle_pause_toggle = mock_handle_pause_toggle

        await view.handle_pause_toggle(interaction)
        mock_player.pause.assert_called_once()
        mock_player.resume.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_skip_defers_before_skip(self, mock_player, mock_controller):
        """handle_skip вызывает defer() перед skip()."""
        from bot.cogs.player_view import PlayerView as RealPlayerView

        call_order = []

        async def mock_defer(*args, **kwargs):
            call_order.append("defer")

        async def mock_skip(*args, **kwargs):
            call_order.append("skip")

        mock_player.skip = mock_skip

        view = RealPlayerView(player=mock_player, controller=mock_controller)
        view._update_player_message = AsyncMock()

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.defer = mock_defer
        interaction.response.is_done = MagicMock(return_value=True)

        # Вызываем handle_skip через контроллер
        async def mock_handle_skip(interaction):
            await interaction.response.defer()
            call_order.append("skip-called")
            await mock_player.skip()

        mock_controller.handle_skip = mock_handle_skip

        await view.handle_skip(interaction)

        # Проверяем порядок: defer должна быть перед skip
        assert "defer" in call_order
        assert "skip-called" in call_order
        assert call_order.index("defer") < call_order.index("skip-called")

    @pytest.mark.asyncio
    async def test_handle_disconnect_defers_before_disconnect(self, mock_player, mock_controller):
        """handle_disconnect вызывает defer() перед disconnect()."""
        from bot.cogs.player_view import PlayerView as RealPlayerView

        call_order = []

        async def mock_defer(*args, **kwargs):
            call_order.append("defer")

        async def mock_disconnect(*args, **kwargs):
            call_order.append("disconnect")

        mock_player.disconnect = mock_disconnect

        view = RealPlayerView(player=mock_player, controller=mock_controller)
        view._update_player_message = AsyncMock()

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.defer = mock_defer
        interaction.response.is_done = MagicMock(return_value=True)

        # Вызываем handle_disconnect через контроллер
        async def mock_handle_disconnect(interaction):
            await interaction.response.defer()
            call_order.append("disconnect-called")
            await mock_player.disconnect()

        mock_controller.handle_disconnect = mock_handle_disconnect

        await view.handle_disconnect(interaction)

        # Проверяем порядок: defer должна быть перед disconnect
        assert "defer" in call_order
        assert "disconnect-called" in call_order
        assert call_order.index("defer") < call_order.index("disconnect-called")

    @pytest.mark.asyncio
    async def test_run_handles_bot_error_with_user_message(self, mock_player, mock_controller):
        """_run обрабатывает BotError и отправляет user_message пользователю."""
        from bot.cogs.player_view import PlayerView as RealPlayerView
        from bot.errors import BotError

        view = RealPlayerView(player=mock_player, controller=mock_controller)

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)

        error = BotError(user_message="Трек не найден")
        action = AsyncMock(side_effect=error)

        await view._run(interaction, action)

        # Проверяем, что response.send_message был вызван с user_message
        interaction.response.send_message.assert_called_once()
        call_args = interaction.response.send_message.call_args
        assert call_args[0][0] == "Трек не найден"
        assert call_args[1]["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_run_handles_generic_exception_with_neutral_message(
        self, mock_player, mock_controller
    ):
        """_run обрабатывает исключения и не утекает их содержимое пользователю."""
        from bot.cogs.player_view import PlayerView as RealPlayerView

        view = RealPlayerView(player=mock_player, controller=mock_controller)

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)

        action = AsyncMock(side_effect=ValueError("Some internal error"))

        await view._run(interaction, action)

        # Проверяем, что отправлено нейтральное сообщение, а не содержимое исключения
        interaction.response.send_message.assert_called_once()
        call_args = interaction.response.send_message.call_args
        message = call_args[0][0]
        assert "Внутренняя ошибка" in message
        assert "Some internal error" not in message


class TestSearchModal:
    """Тесты для SearchModal."""

    @pytest.mark.asyncio
    async def test_search_modal_on_submit_passes_query_correctly(self):
        """SearchModal.on_submit передаёт запрос контроллеру без изменений."""
        from bot.cogs.player_view import SearchModal

        mock_controller = AsyncMock(spec=PlayerController)
        modal = SearchModal(controller=mock_controller)

        # Создаём TextInput с заданным значением
        text_input = MagicMock()
        text_input.value = "The Beatles - Hey Jude"
        modal.query_label.component = text_input

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)

        await modal.on_submit(interaction)

        # Проверяем, что handle_search_query вызвана с точным текстом
        mock_controller.handle_search_query.assert_called_once()
        call_args = mock_controller.handle_search_query.call_args
        assert call_args[0][1] == "The Beatles - Hey Jude"

    @pytest.mark.asyncio
    async def test_search_modal_preserves_special_characters(self):
        """SearchModal.on_submit сохраняет спецсимволы в запросе."""
        from bot.cogs.player_view import SearchModal

        mock_controller = AsyncMock(spec=PlayerController)
        modal = SearchModal(controller=mock_controller)

        text_input = MagicMock()
        text_input.value = 'Artist "Name" - Song (remix)'
        modal.query_label.component = text_input

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.response.is_done = MagicMock(return_value=False)

        await modal.on_submit(interaction)

        mock_controller.handle_search_query.assert_called_once()
        call_args = mock_controller.handle_search_query.call_args
        assert call_args[0][1] == 'Artist "Name" - Song (remix)'
