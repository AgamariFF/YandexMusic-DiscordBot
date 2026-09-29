"""Tests for /search command and TrackSearchView."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import discord.ext.commands
import pytest

from bot.cogs.views import TrackSearchView, _truncate, build_search_embed
from bot.config import Config
from bot.yandex.client import TrackInfo


def create_mock_interaction():
    """Create a basic mock interaction."""
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.response.is_done = MagicMock(return_value=False)
    interaction.response.send_message = AsyncMock()
    interaction.edit_original_response = AsyncMock()
    interaction.followup = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.channel = MagicMock()
    return interaction


def create_mock_interaction_with_member_in_voice():
    """Create a mock interaction with user as Member in a voice channel."""
    interaction = create_mock_interaction()

    member = MagicMock(spec=discord.Member)
    voice_channel = MagicMock(spec=discord.VoiceChannel)
    voice_channel.id = 9999
    voice_state = MagicMock()
    voice_state.channel = voice_channel
    member.voice = voice_state

    interaction.user = member
    return interaction


@pytest.fixture
def mock_config_search():
    """Create a mock Config for search tests."""
    config = MagicMock(spec=Config)
    config.ffmpeg_path = "ffmpeg"
    config.default_volume = 0.5
    config.idle_timeout = 300
    config.guild_id = 12345
    return config


@pytest.fixture
def mock_client_search():
    """Create a mock YandexMusicClient for search tests."""
    return AsyncMock()


@pytest.fixture
def mock_bot_search():
    """Create a mock discord.Bot for search tests."""
    return MagicMock(spec=discord.ext.commands.Bot)


@pytest.fixture
def mock_player_search():
    """Create a mock GuildPlayer for search tests."""
    player = AsyncMock()
    player.voice_client = None
    player.connect = AsyncMock()
    return player


@pytest.fixture
def cog_with_search(mock_bot_search, mock_config_search, mock_client_search, mock_player_search):
    """Create MusicCog with mocked GuildPlayer for search tests."""
    from bot.cogs.music import MusicCog

    with patch("bot.cogs.music.GuildPlayer", return_value=mock_player_search):
        cog = MusicCog(mock_bot_search, mock_config_search, mock_client_search)
    return cog, mock_client_search, mock_player_search


class TestMusicCogSearchCommand:
    """Тесты для команды /search: логика трёх веток (пусто/один/несколько)."""

    @pytest.mark.asyncio
    async def test_search_empty_result_sends_text_message(self, cog_with_search):
        """search с пустым результатом отправляет текстовое сообщение."""
        cog, mock_client, mock_player = cog_with_search
        mock_client.search_tracks = AsyncMock(return_value=())

        interaction = AsyncMock(spec=discord.Interaction)
        interaction.response = AsyncMock()
        interaction.followup = AsyncMock()
        interaction.followup.send = AsyncMock()

        await cog.search.callback(cog, interaction, "nonexistent query")

        interaction.response.defer.assert_called_once()
        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        assert "Ничего не найдено" in call_args[0][0]


class TestTrackSearchViewInteractionCheck:
    """Тесты для TrackSearchView.interaction_check()."""

    @pytest.mark.asyncio
    async def test_interaction_check_allows_author(self):
        """interaction_check разрешает автору команды."""
        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )
        on_select = AsyncMock()
        view = TrackSearchView(tracks=(track,), author_id=12345, on_select=on_select)

        interaction = MagicMock()
        interaction.user = MagicMock()
        interaction.user.id = 12345
        result = await view.interaction_check(interaction)

        assert result is True

    @pytest.mark.asyncio
    async def test_interaction_check_denies_non_author(self):
        """interaction_check запрещает не автору команды."""
        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )
        on_select = AsyncMock()
        view = TrackSearchView(tracks=(track,), author_id=12345, on_select=on_select)

        interaction = AsyncMock()
        interaction.user = MagicMock()
        interaction.user.id = 99999
        interaction.response = AsyncMock()
        interaction.response.send_message = AsyncMock()

        result = await view.interaction_check(interaction)

        assert result is False
        interaction.response.send_message.assert_called_once()
        call_args = interaction.response.send_message.call_args
        assert call_args[1]["ephemeral"] is True


class TestTrackSearchViewSelection:
    """Тесты для выбора трека в TrackSearchView."""

    @pytest.mark.asyncio
    async def test_handle_selection_calls_on_select(self):
        """handle_selection вызывает on_select с правильным треком."""
        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )
        on_select = AsyncMock()
        view = TrackSearchView(tracks=(track,), author_id=12345, on_select=on_select)

        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.response.edit_message = AsyncMock()

        await view.handle_selection(interaction, track)

        on_select.assert_called_once()
        call_args = on_select.call_args
        assert call_args[0][1] == track


class TestTrackButtonLabels:
    """Тесты для подписей и поведения кнопок TrackButton."""

    def test_track_buttons_created_for_each_track(self):
        """Для каждого трека создаётся кнопка."""
        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist 1",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track3",
                feedback_id="track3:album3",
                title="Song 3",
                artists="Artist 3",
                duration=220.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        on_select = AsyncMock()
        view = TrackSearchView(
            tracks=tracks, author_id=12345, on_select=on_select
        )

        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        assert len(buttons) == len(tracks), (
            f"Должно быть {len(tracks)} кнопок, получено {len(buttons)}"
        )

    def test_button_labels_are_numbers_starting_from_one(self):
        """Подписи кнопок - номера начиная с 1."""
        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist 1",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        on_select = AsyncMock()
        view = TrackSearchView(
            tracks=tracks, author_id=12345, on_select=on_select
        )

        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        assert len(buttons) == 2

        assert buttons[0].label == "1", (
            f"Первая кнопка должна быть '1', получено {buttons[0].label}"
        )
        assert buttons[1].label == "2", (
            f"Вторая кнопка должна быть '2', получено {buttons[1].label}"
        )

    def test_truncate_function_respects_limit(self):
        """_truncate функция обрезает текст до лимита с многоточием."""
        long_text = "A" * 150
        result = _truncate(long_text, limit=100)

        assert len(result) <= 100
        assert result.endswith("…")

    def test_truncate_function_preserves_short_text(self):
        """_truncate функция не изменяет короткий текст."""
        short_text = "Hello"
        result = _truncate(short_text, limit=100)

        assert result == short_text


class TestSearchEmbedBuild:
    """Тесты для build_search_embed."""

    def test_build_search_embed_contains_numbered_list(self):
        """build_search_embed содержит нумерованный список треков."""
        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist 1",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        embed = build_search_embed("test query", tracks, 0x3498db)

        assert embed.description is not None
        assert "**1.**" in embed.description, "Первый трек должен быть с номером 1"
        assert "**2.**" in embed.description, "Второй трек должен быть с номером 2"
        assert "Artist 1" in embed.description
        assert "Song 1" in embed.description
        assert "Artist 2" in embed.description
        assert "Song 2" in embed.description

    def test_build_search_embed_title_contains_query(self):
        """Заголовок embed содержит запрос пользователя."""
        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song",
                artists="Artist",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        query = "my search query"
        embed = build_search_embed(query, tracks, 0x3498db)

        assert query in embed.title, f"Запрос '{query}' должен быть в заголовке '{embed.title}'"

    def test_max_search_results_limit_is_five(self):
        """MAX_SEARCH_RESULTS равен 5."""
        from bot.cogs.views import MAX_SEARCH_RESULTS

        assert MAX_SEARCH_RESULTS == 5, (
            f"MAX_SEARCH_RESULTS должен быть 5, получено {MAX_SEARCH_RESULTS}"
        )

    @pytest.mark.asyncio
    async def test_button_click_third_track_calls_on_select(self):
        """Нажатие на третью кнопку запускает on_select с третьим треком."""

        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist 1",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track3",
                feedback_id="track3:album3",
                title="Song 3",
                artists="Artist 3",
                duration=220.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        on_select = AsyncMock()
        view = TrackSearchView(
            tracks=tracks, author_id=12345, on_select=on_select
        )

        # Находим третью кнопку (с label="3")
        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        third_button = buttons[2]
        assert third_button.label == "3"

        # Имитируем нажатие на третью кнопку
        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup = AsyncMock()
        interaction.user = MagicMock()
        interaction.user.id = 12345

        await third_button.callback(interaction)

        # Проверяем что on_select был вызван с третьим треком
        on_select.assert_called_once()
        call_args = on_select.call_args
        selected_track = call_args[0][1]
        assert selected_track.id == "track3", (
            f"Должен быть трек 3, получен {selected_track.id}"
        )


class TestTrackSearchViewTimeout:
    """Тесты для таймаута TrackSearchView."""

    @pytest.mark.asyncio
    async def test_on_timeout_disables_buttons(self):
        """on_timeout отключает кнопки."""
        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )
        on_select = AsyncMock()
        view = TrackSearchView(tracks=tracks, author_id=12345, on_select=on_select)

        message = MagicMock()
        message.edit = AsyncMock()
        view.attach_message(message)

        await view.on_timeout()

        # Проверяем что все кнопки отключены
        buttons = [item for item in view.children if isinstance(item, discord.ui.Button)]
        assert len(buttons) > 0, "Должны быть кнопки"
        for button in buttons:
            assert button.disabled is True, f"Кнопка с label={button.label} должна быть отключена"
        message.edit.assert_called_once()


class TestSearchCommandBranching:
    """Тесты для ветвления команды /search (один трек vs несколько)."""

    @pytest.mark.asyncio
    async def test_search_single_track_starts_wave_no_menu(self, cog_with_search):
        """Поиск одного трека запускает волну сразу без меню."""
        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="single_track",
            feedback_id="single_track:album1",
            title="Only Song",
            artists="Solo Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )

        mock_client.search_tracks = AsyncMock(return_value=(track,))
        mock_player.start_wave_from_track = AsyncMock(return_value=track)

        interaction = create_mock_interaction_with_member_in_voice()

        await cog.search.callback(cog, interaction, "single")

        mock_player.start_wave_from_track.assert_called_once_with(track)

        for call in interaction.followup.send.call_args_list:
            if "view" in call[1]:
                pytest.fail("Меню не должно создаваться для одного найденного трека")

    @pytest.mark.asyncio
    async def test_search_multiple_tracks_creates_menu_no_immediate_wave(
        self, cog_with_search
    ):
        """Поиск нескольких треков создаёт меню, волна не запускается сразу."""
        cog, mock_client, mock_player = cog_with_search

        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist 1",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track3",
                feedback_id="track3:album3",
                title="Song 3",
                artists="Artist 3",
                duration=220.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        mock_client.search_tracks = AsyncMock(return_value=tracks)
        mock_player.start_wave_from_track = AsyncMock()

        message_mock = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()
        interaction.followup.send = AsyncMock(return_value=message_mock)

        await cog.search.callback(cog, interaction, "multiple")

        mock_player.start_wave_from_track.assert_not_called()

        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        assert "view" in call_args[1]
        view = call_args[1]["view"]
        assert isinstance(view, TrackSearchView)

        # Проверяем что подключения к каналу не произошло - бот не должен
        # заходить в канал, пока пользователь не выберет трек из меню
        mock_player.connect.assert_not_called()

        # Проверяем что embed отправлен и содержит все найденные треки
        assert "embed" in call_args[1], "embed должен быть передан в followup.send"
        embed = call_args[1]["embed"]

        assert embed.description is not None
        # Проверяем что все три трека в описании с номерами
        assert "**1.**" in embed.description and "Artist 1" in embed.description and (
            "Song 1" in embed.description
        ), "Первый трек должен быть в описании"
        assert "**2.**" in embed.description and "Artist 2" in embed.description and (
            "Song 2" in embed.description
        ), "Второй трек должен быть в описании"
        assert "**3.**" in embed.description and "Artist 3" in embed.description and (
            "Song 3" in embed.description
        ), "Третий трек должен быть в описании"


class TestMusicCogWaveDescription:
    """Тесты для отображения описания волны в embed'ах."""

    @pytest.mark.asyncio
    async def test_announce_updates_player_message(self, cog_with_search):
        """_announce обновляет сообщение-плеер при новом треке."""
        from unittest.mock import patch

        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Test Song",
            artists="Test Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )

        # Мокируем _update_player_message
        with patch.object(cog, '_update_player_message', new_callable=AsyncMock) as mock_update:
            # Вызываем _announce с описанием волны
            wave_desc = "Моя волна по Test Artist — Test Song"
            mock_player.wave_description = wave_desc
            await cog._announce(track, wave_desc)

        # Проверяем что _update_player_message был вызван
        mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_announce_wave_description_none_updates_message(self, cog_with_search):
        """_announce обновляет плеер даже при wave_description=None."""
        from unittest.mock import patch

        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Test Song",
            artists="Test Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )

        # Мокируем _update_player_message
        with patch.object(cog, '_update_player_message', new_callable=AsyncMock) as mock_update:
            mock_player.wave_description = None
            await cog._announce(track, None)

        # Проверяем что _update_player_message был вызван
        mock_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_player_embed_includes_wave_description(self, cog_with_search):
        """build_player_embed включает описание волны в заголовок."""
        from bot.cogs.music import build_player_embed

        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Started Track",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )

        # Устанавливаем wave_description в плеере
        wave_desc = "Моя волна по Artist — Track"
        mock_player.wave_description = wave_desc
        mock_player.current = track
        mock_player.state = 1  # PLAYING
        mock_player.now_playing = MagicMock(return_value=MagicMock(elapsed=50.0))

        # Собираем embed
        embed = build_player_embed(mock_player, discord.Color.gold())

        # Описание волны должно быть в заголовке
        assert embed.title == wave_desc

    @pytest.mark.asyncio
    async def test_nowplaying_includes_wave_description(self, cog_with_search):
        """Команда /nowplaying выводит поле "Волна" с текущим описанием."""
        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="current_track",
            feedback_id="current_track:album1",
            title="Now Playing",
            artists="Current Artist",
            duration=240.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )

        # Мокируем now_playing() на плеере
        from bot.player import NowPlaying

        now_playing = NowPlaying(
            track=track,
            elapsed=120.0,
            volume=0.75,
            paused=False,
            bass=MagicMock(label="Нет"),
        )
        mock_player.now_playing = MagicMock(return_value=now_playing)

        # Устанавливаем wave_description
        wave_desc = "Моя волна по Current Artist — Now Playing"
        mock_player.wave_description = wave_desc

        interaction = create_mock_interaction_with_member_in_voice()

        await cog.nowplaying.callback(cog, interaction)

        interaction.response.send_message.assert_called_once()
        call_args = interaction.response.send_message.call_args
        embed = call_args[1]["embed"]

        wave_field = None
        for field in embed.fields:
            if field.name == "Волна":
                wave_field = field
                break

        assert wave_field is not None, "Поле 'Волна' должно присутствовать в /nowplaying"
        assert wave_field.value == wave_desc, (
            f"Значение 'Волна' должно быть '{wave_desc}', получено {wave_field.value}"
        )


class TestAllowedMentionsProtection:
    """Тесты для защиты от пинга ролей через user-controlled text."""

    @pytest.mark.asyncio
    async def test_search_empty_result_has_allowed_mentions_none(self, cog_with_search):
        """Сообщение 'Ничего не найдено' содержит allowed_mentions=none()."""
        cog, mock_client, mock_player = cog_with_search
        mock_client.search_tracks = AsyncMock(return_value=())

        interaction = create_mock_interaction()

        await cog.search.callback(cog, interaction, "nonexistent")

        interaction.response.defer.assert_called_once()
        interaction.followup.send.assert_called_once()

        call_args = interaction.followup.send.call_args
        assert "allowed_mentions" in call_args[1], (
            "Параметр allowed_mentions должен быть передан"
        )

        allowed_mentions = call_args[1]["allowed_mentions"]
        assert allowed_mentions is not None
        assert allowed_mentions.everyone is False, (
            f"allowed_mentions.everyone должен быть False, получено {allowed_mentions.everyone}"
        )
        assert allowed_mentions.roles is False, (
            f"allowed_mentions.roles должен быть False, получено {allowed_mentions.roles}"
        )
        assert allowed_mentions.users is False, (
            f"allowed_mentions.users должен быть False, получено {allowed_mentions.users}"
        )

    @pytest.mark.asyncio
    async def test_search_multiple_tracks_menu_has_allowed_mentions_none(
        self, cog_with_search
    ):
        """Сообщение с меню содержит allowed_mentions=none()."""
        cog, mock_client, mock_player = cog_with_search

        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Song 1",
                artists="Artist 1",
                duration=180.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
            cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        mock_client.search_tracks = AsyncMock(return_value=tracks)
        mock_player.start_wave_from_track = AsyncMock()

        message_mock = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()
        interaction.followup.send = AsyncMock(return_value=message_mock)

        await cog.search.callback(cog, interaction, "multiple")

        interaction.followup.send.assert_called_once()

        call_args = interaction.followup.send.call_args
        assert "allowed_mentions" in call_args[1], (
            "Параметр allowed_mentions должен быть передан для меню"
        )

        allowed_mentions = call_args[1]["allowed_mentions"]
        assert allowed_mentions is not None
        assert allowed_mentions.everyone is False
        assert allowed_mentions.roles is False
        assert allowed_mentions.users is False

    @pytest.mark.asyncio
    async def test_track_selection_deletes_menu_after_choice(self):
        """handle_selection удаляет меню после выбора и запускает волну."""
        track = TrackInfo(
            id="selected_track",
            feedback_id="selected_track:album1",
            title="Selected Song",
            artists="Selected Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )
        on_select = AsyncMock()
        view = TrackSearchView(
            tracks=(track,), author_id=12345, on_select=on_select
        )

        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.delete_original_response = AsyncMock()

        await view.handle_selection(interaction, track)

        # Проверяем, что взаимодействие было отложено
        interaction.response.defer.assert_called_once()
        # Проверяем, что исходное сообщение (меню) было удалено
        interaction.delete_original_response.assert_called_once()
        # Проверяем, что on_select был вызван (он получает interaction и track)
        on_select.assert_called_once()


def _create_expired_not_found() -> discord.NotFound:
    """Create discord.NotFound with code 10062 (expired interaction window)."""
    response_mock = MagicMock()
    response_mock.status = 404
    message_dict = {"code": 10062, "message": "Unknown interaction"}
    return discord.NotFound(response_mock, message_dict)


class TestTrackSearchViewExpiredInteraction:
    """Тесты для TrackSearchView при истекшем окне interaction (код 10062)."""

    @pytest.mark.asyncio
    async def test_handle_selection_recovers_from_expired_window_and_stays_alive(self):
        """После истечения окна меню остаётся живым и второй клик работает.

        Сценарий: первый клик имеет истекшее окно (10062), второй клик нормальный.
        Ожидаемо: первый клик не бросает исключение, on_select не вызывается,
        после первого клика меню работает, второй клик вызывает on_select ровно один раз.
        """
        tracks = (
            TrackInfo(
                id="track1",
                feedback_id="track1:album1",
                title="Test Song",
                artists="Test Artist",
                duration=180.0,
                cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Another Song",
                artists="Another Artist",
                duration=200.0,
                cover_url="https://example.com/cover.jpg",
                raw=None,
            ),
        )

        on_select = AsyncMock()
        view = TrackSearchView(tracks=tracks, author_id=12345, on_select=on_select)

        # === First interaction: expired window ===
        expired_interaction = AsyncMock()
        expired_interaction.response = AsyncMock()
        expired_interaction.response.defer = AsyncMock(
            side_effect=_create_expired_not_found()
        )
        expired_interaction.channel = MagicMock()
        expired_interaction.channel.send = AsyncMock()

        # First click should not raise an exception
        await view.handle_selection(expired_interaction, tracks[0])

        # on_select should NOT have been called
        on_select.assert_not_called()

        # A warning message should have been sent to the channel
        expired_interaction.channel.send.assert_called_once()
        warning_message = expired_interaction.channel.send.call_args[0][0]
        assert warning_message is not None

        # === Second interaction: normal, after expired ===
        normal_interaction = AsyncMock()
        normal_interaction.response = AsyncMock()
        normal_interaction.response.defer = AsyncMock()
        normal_interaction.response.edit_message = AsyncMock()
        normal_interaction.delete_original_response = AsyncMock()

        # Second click should work normally
        await view.handle_selection(normal_interaction, tracks[1])

        # on_select should now be called exactly once (from the second click)
        on_select.assert_called_once()
        call_args = on_select.call_args
        selected_track = call_args[0][1]
        assert selected_track.id == "track2", (
            f"Expected track2, got {selected_track.id}"
        )
