"""Tests for /search command and TrackSearchView."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import discord.ext.commands
import pytest

from bot.cogs.views import TrackSearchView, TrackSelect, _truncate
from bot.config import Config
from bot.yandex.client import TrackInfo


def create_mock_interaction():
    """Create a basic mock interaction."""
    interaction = AsyncMock(spec=discord.Interaction)
    interaction.response = AsyncMock()
    interaction.response.defer = AsyncMock()
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


class TestTrackSelectTruncation:
    """Тесты для обрезки длинных названий в TrackSelect."""

    def test_track_select_truncates_long_label(self):
        """TrackSelect обрезает длинное название до 100 символов."""
        long_artist = "A" * 60
        long_title = "B" * 60
        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title=long_title,
            artists=long_artist,
            duration=180.0,
            raw=None,
        )

        select = TrackSelect((track,))
        option = select.options[0]

        assert len(option.label) <= 100
        full_text = f"{long_artist} — {long_title}"
        if len(full_text) > 100:
            assert option.label.endswith("…")

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


class TestTrackSearchViewTimeout:
    """Тесты для таймаута TrackSearchView."""

    @pytest.mark.asyncio
    async def test_on_timeout_disables_items(self):
        """on_timeout отключает компоненты меню."""
        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Song",
            artists="Artist",
            duration=180.0,
            raw=None,
        )
        on_select = AsyncMock()
        view = TrackSearchView(tracks=(track,), author_id=12345, on_select=on_select)

        message = MagicMock()
        message.edit = AsyncMock()
        view.attach_message(message)

        await view.on_timeout()

        for item in view.children:
            if hasattr(item, "disabled"):
                assert item.disabled is True
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
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
                raw=None,
            ),
            TrackInfo(
                id="track3",
                feedback_id="track3:album3",
                title="Song 3",
                artists="Artist 3",
                duration=220.0,
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


class TestMusicCogWaveDescription:
    """Тесты для отображения описания волны в embed'ах."""

    @pytest.mark.asyncio
    async def test_announce_includes_wave_description_field(self, cog_with_search):
        """Анонс нового трека отправляет embed с полем "Волна"."""
        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Test Song",
            artists="Test Artist",
            duration=180.0,
            raw=None,
        )

        # Мокируем канал анонса
        mock_channel = AsyncMock()
        cog._announce_channel = mock_channel

        # Вызываем _announce с конкретным описанием волны
        wave_desc = "Моя волна по Test Artist — Test Song"
        await cog._announce(track, wave_desc)

        # Проверяем что было отправлено сообщение
        mock_channel.send.assert_called_once()
        call_args = mock_channel.send.call_args
        embed = call_args[1]["embed"]

        # Проверяем что embed содержит поле "Волна" с правильным значением
        assert embed is not None
        wave_field = None
        for field in embed.fields:
            if field.name == "Волна":
                wave_field = field
                break

        assert wave_field is not None, "Поле 'Волна' должно присутствовать в embed"
        assert wave_field.value == wave_desc, (
            f"Значение 'Волна' должно быть '{wave_desc}', получено {wave_field.value}"
        )

    @pytest.mark.asyncio
    async def test_announce_wave_description_none_shows_fallback(self, cog_with_search):
        """При wave_description=None показывается запасное значение '—'."""
        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Test Song",
            artists="Test Artist",
            duration=180.0,
            raw=None,
        )

        mock_channel = AsyncMock()
        cog._announce_channel = mock_channel

        # Вызываем с wave_description=None
        await cog._announce(track, None)

        mock_channel.send.assert_called_once()
        call_args = mock_channel.send.call_args
        embed = call_args[1]["embed"]

        wave_field = None
        for field in embed.fields:
            if field.name == "Волна":
                wave_field = field
                break

        assert wave_field is not None
        assert wave_field.value == "—", (
            f"При None wave_description должен быть '—', получено {wave_field.value}"
        )

    @pytest.mark.asyncio
    async def test_send_wave_started_includes_wave_description(self, cog_with_search):
        """_send_wave_started берёт wave_description и помещает в embed."""
        cog, mock_client, mock_player = cog_with_search

        track = TrackInfo(
            id="track1",
            feedback_id="track1:album1",
            title="Started Track",
            artists="Artist",
            duration=180.0,
            raw=None,
        )

        # Устанавливаем wave_description в плеере
        wave_desc = "Моя волна по Artist — Track"
        mock_player.wave_description = wave_desc

        interaction = create_mock_interaction_with_member_in_voice()

        await cog._send_wave_started(interaction, track)

        interaction.followup.send.assert_called_once()
        call_args = interaction.followup.send.call_args
        embed = call_args[1]["embed"]

        wave_field = None
        for field in embed.fields:
            if field.name == "Волна":
                wave_field = field
                break

        assert wave_field is not None, "Поле 'Волна' должно присутствовать в embed"
        assert wave_field.value == wave_desc, (
            f"Значение 'Волна' должно быть '{wave_desc}', получено {wave_field.value}"
        )

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
                raw=None,
            ),
            TrackInfo(
                id="track2",
                feedback_id="track2:album2",
                title="Song 2",
                artists="Artist 2",
                duration=200.0,
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
    async def test_track_selection_edit_message_has_allowed_mentions_none(self):
        """edit_message в handle_selection содержит allowed_mentions=none()."""
        track = TrackInfo(
            id="selected_track",
            feedback_id="selected_track:album1",
            title="Selected Song",
            artists="Selected Artist",
            duration=180.0,
            raw=None,
        )
        on_select = AsyncMock()
        view = TrackSearchView(
            tracks=(track,), author_id=12345, on_select=on_select
        )

        interaction = AsyncMock()
        interaction.response = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup = AsyncMock()

        await view.handle_selection(interaction, track)

        interaction.response.edit_message.assert_called_once()

        call_args = interaction.response.edit_message.call_args
        assert "allowed_mentions" in call_args[1], (
            "Параметр allowed_mentions должен быть передан для edit_message"
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
