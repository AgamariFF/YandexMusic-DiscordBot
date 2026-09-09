"""Tests for bot.cogs.music commands: /wave and /play."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from bot.cogs.music import MusicCog
from bot.config import Config
from bot.errors import NotInVoiceChannelError
from bot.yandex import TrackInfo


@pytest.fixture
def mock_config():
    """Create a mock Config."""
    config = MagicMock(spec=Config)
    config.ffmpeg_path = "ffmpeg"
    config.default_volume = 0.5
    config.idle_timeout = 300
    config.guild_id = 12345
    return config


@pytest.fixture
def mock_client():
    """Create a mock YandexMusicClient."""
    return AsyncMock()


@pytest.fixture
def mock_bot():
    """Create a mock discord.Bot."""
    return MagicMock(spec=discord.ext.commands.Bot)


@pytest.fixture
def mock_player():
    """Create a mock GuildPlayer."""
    player = AsyncMock()
    player.voice_client = None
    player.connect = AsyncMock()
    player.start_wave = AsyncMock(
        return_value=TrackInfo(
            id="track_123",
            feedback_id="track_123:album_456",
            title="Test Track",
            artists="Test Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=None,
        )
    )
    return player


@pytest.fixture
def cog_with_mocked_player(mock_bot, mock_config, mock_client, mock_player):
    """Create MusicCog with mocked GuildPlayer.

    Patches the GuildPlayer constructor to return our mock_player.
    """
    with patch("bot.cogs.music.GuildPlayer", return_value=mock_player):
        cog = MusicCog(mock_bot, mock_config, mock_client)
    # Verify the player was created and assigned
    assert cog.player is mock_player
    return cog


class TestMusicCogCommandsRegistration:
    """Tests for /wave and /play command registration."""

    def test_wave_command_exists(self, cog_with_mocked_player):
        """MusicCog.wave command exists."""
        assert hasattr(cog_with_mocked_player, "wave")
        assert cog_with_mocked_player.wave is not None

    def test_play_command_exists(self, cog_with_mocked_player):
        """MusicCog.play command exists."""
        assert hasattr(cog_with_mocked_player, "play")
        assert cog_with_mocked_player.play is not None

    def test_wave_is_app_command(self, cog_with_mocked_player):
        """MusicCog.wave is an app_commands.Command."""
        from discord import app_commands

        assert isinstance(cog_with_mocked_player.wave, app_commands.Command)

    def test_play_is_app_command(self, cog_with_mocked_player):
        """MusicCog.play is an app_commands.Command."""
        from discord import app_commands

        assert isinstance(cog_with_mocked_player.play, app_commands.Command)

    def test_wave_command_name(self, cog_with_mocked_player):
        """MusicCog.wave command has name 'wave'."""
        assert cog_with_mocked_player.wave.name == "wave"

    def test_play_command_name(self, cog_with_mocked_player):
        """MusicCog.play command has name 'play'."""
        assert cog_with_mocked_player.play.name == "play"

    def test_wave_command_description(self, cog_with_mocked_player):
        """MusicCog.wave command has correct description."""
        assert cog_with_mocked_player.wave.description == "Запустить «Мою волну»"

    def test_play_command_description(self, cog_with_mocked_player):
        """MusicCog.play command has correct description."""
        assert cog_with_mocked_player.play.description == "Запустить «Мою волну»"

    def test_wave_command_is_guild_only(self, cog_with_mocked_player):
        """MusicCog.wave command is guild_only."""
        wave_cmd = cog_with_mocked_player.wave
        assert hasattr(wave_cmd, "guild_only") and wave_cmd.guild_only is True

    def test_play_command_is_guild_only(self, cog_with_mocked_player):
        """MusicCog.play command is guild_only."""
        play_cmd = cog_with_mocked_player.play
        assert hasattr(play_cmd, "guild_only") and play_cmd.guild_only is True

    def test_both_commands_registered_in_cog(self, cog_with_mocked_player):
        """Both /wave and /play are registered as separate cog app commands."""
        names = [c.name for c in cog_with_mocked_player.get_app_commands()]
        assert "wave" in names and "play" in names
        assert len(names) == len(set(names))


class TestMusicCogWavePlayHappyPath:
    """Tests for /wave and /play happy path behavior."""

    @pytest.mark.asyncio
    async def test_wave_connects_when_voice_client_none(self, cog_with_mocked_player, mock_player):
        """wave command connects to voice channel when voice_client is None."""
        mock_player.voice_client = None
        interaction = create_mock_interaction_with_member_in_voice()

        await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

        mock_player.connect.assert_called_once()
        channel = mock_player.connect.call_args[0][0]
        assert channel.id == 9999

    @pytest.mark.asyncio
    async def test_play_connects_when_voice_client_none(self, cog_with_mocked_player, mock_player):
        """play command connects to voice channel when voice_client is None."""
        mock_player.voice_client = None
        interaction = create_mock_interaction_with_member_in_voice()

        await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

        mock_player.connect.assert_called_once()
        channel = mock_player.connect.call_args[0][0]
        assert channel.id == 9999

    @pytest.mark.asyncio
    async def test_wave_does_not_connect_when_already_connected(
        self, cog_with_mocked_player, mock_player
    ):
        """wave command does not call connect when already connected."""
        mock_player.voice_client = MagicMock()  # Not None
        interaction = create_mock_interaction_with_member_in_voice()

        await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

        mock_player.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_play_does_not_connect_when_already_connected(
        self, cog_with_mocked_player, mock_player
    ):
        """play command does not call connect when already connected."""
        mock_player.voice_client = MagicMock()  # Not None
        interaction = create_mock_interaction_with_member_in_voice()

        await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

        mock_player.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_wave_saves_announce_channel(self, cog_with_mocked_player, mock_player):
        """wave command saves interaction.channel as announce channel."""
        mock_player.voice_client = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()
        expected_channel = MagicMock()
        interaction.channel = expected_channel

        await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

        assert cog_with_mocked_player._announce_channel is expected_channel

    @pytest.mark.asyncio
    async def test_play_saves_announce_channel(self, cog_with_mocked_player, mock_player):
        """play command saves interaction.channel as announce channel."""
        mock_player.voice_client = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()
        expected_channel = MagicMock()
        interaction.channel = expected_channel

        await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

        assert cog_with_mocked_player._announce_channel is expected_channel

    @pytest.mark.asyncio
    async def test_wave_calls_start_wave(self, cog_with_mocked_player, mock_player):
        """wave command calls start_wave() to begin playback."""
        mock_player.voice_client = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()

        with patch.object(cog_with_mocked_player, '_update_player_message', new_callable=AsyncMock):
            await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

        mock_player.start_wave.assert_called_once()

    @pytest.mark.asyncio
    async def test_play_calls_start_wave(self, cog_with_mocked_player, mock_player):
        """play command calls start_wave() to begin playback."""
        mock_player.voice_client = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()

        with patch.object(cog_with_mocked_player, '_update_player_message', new_callable=AsyncMock):
            await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

        mock_player.start_wave.assert_called_once()

    @pytest.mark.asyncio
    async def test_wave_defers_response(self, cog_with_mocked_player, mock_player):
        """wave command defers response while loading wave."""
        mock_player.voice_client = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()

        with patch.object(cog_with_mocked_player, '_update_player_message', new_callable=AsyncMock):
            await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

        interaction.response.defer.assert_called_once()

    @pytest.mark.asyncio
    async def test_play_defers_response(self, cog_with_mocked_player, mock_player):
        """play command defers response while loading wave."""
        mock_player.voice_client = MagicMock()
        interaction = create_mock_interaction_with_member_in_voice()

        with patch.object(cog_with_mocked_player, '_update_player_message', new_callable=AsyncMock):
            await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

        interaction.response.defer.assert_called_once()


class TestMusicCogWavePlayUserNotInVoiceChannel:
    """Tests for /wave and /play when user is not in voice channel."""

    @pytest.mark.asyncio
    async def test_wave_raises_not_in_voice_channel_when_user_not_member(
        self, cog_with_mocked_player, mock_player
    ):
        """wave command raises NotInVoiceChannelError when user is not a Member."""
        mock_player.voice_client = None
        interaction = create_mock_interaction()
        interaction.user = MagicMock(spec=discord.User)  # Not a Member

        with pytest.raises(NotInVoiceChannelError):
            await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

    @pytest.mark.asyncio
    async def test_play_raises_not_in_voice_channel_when_user_not_member(
        self, cog_with_mocked_player, mock_player
    ):
        """play command raises NotInVoiceChannelError when user is not a Member."""
        mock_player.voice_client = None
        interaction = create_mock_interaction()
        interaction.user = MagicMock(spec=discord.User)  # Not a Member

        with pytest.raises(NotInVoiceChannelError):
            await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

    @pytest.mark.asyncio
    async def test_wave_raises_not_in_voice_channel_when_user_voice_none(
        self, cog_with_mocked_player, mock_player
    ):
        """wave command raises NotInVoiceChannelError when member.voice is None."""
        mock_player.voice_client = None
        interaction = create_mock_interaction()
        member = MagicMock(spec=discord.Member)
        member.voice = None
        interaction.user = member

        with pytest.raises(NotInVoiceChannelError):
            await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

    @pytest.mark.asyncio
    async def test_play_raises_not_in_voice_channel_when_user_voice_none(
        self, cog_with_mocked_player, mock_player
    ):
        """play command raises NotInVoiceChannelError when member.voice is None."""
        mock_player.voice_client = None
        interaction = create_mock_interaction()
        member = MagicMock(spec=discord.Member)
        member.voice = None
        interaction.user = member

        with pytest.raises(NotInVoiceChannelError):
            await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)

    @pytest.mark.asyncio
    async def test_wave_raises_not_in_voice_channel_when_channel_none(
        self, cog_with_mocked_player, mock_player
    ):
        """wave command raises NotInVoiceChannelError when member.voice.channel is None."""
        mock_player.voice_client = None
        interaction = create_mock_interaction()
        member = MagicMock(spec=discord.Member)
        voice_state = MagicMock()
        voice_state.channel = None
        member.voice = voice_state
        interaction.user = member

        with pytest.raises(NotInVoiceChannelError):
            await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction)

    @pytest.mark.asyncio
    async def test_play_raises_not_in_voice_channel_when_channel_none(
        self, cog_with_mocked_player, mock_player
    ):
        """play command raises NotInVoiceChannelError when member.voice.channel is None."""
        mock_player.voice_client = None
        interaction = create_mock_interaction()
        member = MagicMock(spec=discord.Member)
        voice_state = MagicMock()
        voice_state.channel = None
        member.voice = voice_state
        interaction.user = member

        with pytest.raises(NotInVoiceChannelError):
            await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction)


class TestMusicCogWavePlayIdempotent:
    """Tests for idempotency of /wave and /play commands."""

    @pytest.mark.asyncio
    async def test_wave_and_play_identical_behavior_same_state(
        self, cog_with_mocked_player, mock_player
    ):
        """wave and play commands produce identical behavior in same state."""
        mock_player.voice_client = None

        # Reset mocks between calls
        def create_interaction():
            return create_mock_interaction_with_member_in_voice()

        # Call wave
        interaction_wave = create_interaction()
        with patch.object(cog_with_mocked_player, '_update_player_message', new_callable=AsyncMock):
            await cog_with_mocked_player.wave.callback(cog_with_mocked_player, interaction_wave)
        wave_connect_calls = mock_player.connect.call_count
        wave_start_wave_calls = mock_player.start_wave.call_count

        # Reset for play
        mock_player.connect.reset_mock()
        mock_player.start_wave.reset_mock()
        mock_player.voice_client = None

        # Call play
        interaction_play = create_interaction()
        with patch.object(cog_with_mocked_player, '_update_player_message', new_callable=AsyncMock):
            await cog_with_mocked_player.play.callback(cog_with_mocked_player, interaction_play)
        play_connect_calls = mock_player.connect.call_count
        play_start_wave_calls = mock_player.start_wave.call_count

        # Both should call connect
        assert wave_connect_calls == 1
        assert play_connect_calls == 1
        # Both should call start_wave
        assert wave_start_wave_calls == 1
        assert play_start_wave_calls == 1


# Helper functions


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

    # Create a mock Member with voice state
    member = MagicMock(spec=discord.Member)
    voice_channel = MagicMock(spec=discord.VoiceChannel)
    voice_channel.id = 9999
    voice_state = MagicMock()
    voice_state.channel = voice_channel
    member.voice = voice_state

    interaction.user = member
    return interaction


class TestMusicCogHandlePauseToggle:
    """Тесты для MusicCog.handle_pause_toggle."""

    @pytest.fixture
    def cog_with_player(self, mock_config, mock_client, mock_bot):
        """MusicCog с реальным плеером."""
        from bot.player import GuildPlayer, PlayerState

        cog = MusicCog(mock_bot, mock_config, mock_client)
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.current = None
        cog._player.state = PlayerState.IDLE
        cog._player.resume = MagicMock()
        cog._player.pause = MagicMock()
        cog._update_player_message = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_handle_pause_toggle_resumes_when_paused(self, cog_with_player):
        """handle_pause_toggle вызывает resume() при PAUSED."""
        from bot.player import PlayerState

        cog_with_player._player.state = PlayerState.PAUSED

        interaction = create_mock_interaction()
        await cog_with_player.handle_pause_toggle(interaction)

        cog_with_player._player.resume.assert_called_once()
        cog_with_player._player.pause.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_pause_toggle_pauses_when_playing(self, cog_with_player):
        """handle_pause_toggle вызывает pause() при PLAYING."""
        from bot.player import PlayerState

        cog_with_player._player.state = PlayerState.PLAYING

        interaction = create_mock_interaction()
        await cog_with_player.handle_pause_toggle(interaction)

        cog_with_player._player.pause.assert_called_once()
        cog_with_player._player.resume.assert_not_called()


class TestMusicCogHandleSkip:
    """Тесты для MusicCog.handle_skip."""

    @pytest.fixture
    def cog_with_player(self, mock_config, mock_client, mock_bot):
        """MusicCog с реальным плеером."""
        from bot.player import GuildPlayer

        cog = MusicCog(mock_bot, mock_config, mock_client)
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.skip = AsyncMock()
        cog._update_player_message = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_handle_skip_defers_before_skip(self, cog_with_player):
        """handle_skip вызывает defer() перед skip()."""
        call_order = []

        async def mock_defer(*args, **kwargs):
            call_order.append("defer")

        async def mock_skip(*args, **kwargs):
            call_order.append("skip")

        cog_with_player._player.skip = mock_skip

        interaction = create_mock_interaction()
        interaction.response.defer = mock_defer

        await cog_with_player.handle_skip(interaction)

        assert "defer" in call_order
        assert "skip" in call_order
        assert call_order.index("defer") < call_order.index("skip")


class TestMusicCogHandleDisconnect:
    """Тесты для MusicCog.handle_disconnect."""

    @pytest.fixture
    def cog_with_player(self, mock_config, mock_client, mock_bot):
        """MusicCog с реальным плеером."""
        from bot.player import GuildPlayer

        cog = MusicCog(mock_bot, mock_config, mock_client)
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.disconnect = AsyncMock()
        cog._update_player_message = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_handle_disconnect_defers_before_disconnect(self, cog_with_player):
        """handle_disconnect вызывает defer() перед disconnect()."""
        call_order = []

        async def mock_defer(*args, **kwargs):
            call_order.append("defer")

        async def mock_disconnect(*args, **kwargs):
            call_order.append("disconnect")

        cog_with_player._player.disconnect = mock_disconnect

        interaction = create_mock_interaction()
        interaction.response.defer = mock_defer

        await cog_with_player.handle_disconnect(interaction)

        assert "defer" in call_order
        assert "disconnect" in call_order
        assert call_order.index("defer") < call_order.index("disconnect")


class TestMusicCogHandleSearchQuery:
    """Тесты для MusicCog.handle_search_query."""

    @pytest.fixture
    def cog_with_player(self, mock_config, mock_client, mock_bot):
        """MusicCog с реальным плеером."""
        cog = MusicCog(mock_bot, mock_config, mock_client)
        cog._search_and_start = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_handle_search_query_transmits_exact_query(self, cog_with_player):
        """handle_search_query передаёт query без изменений в _search_and_start."""
        query_received = None

        async def capture_query(interaction, received_query):
            nonlocal query_received
            query_received = received_query

        cog_with_player._search_and_start = capture_query

        interaction = create_mock_interaction()
        test_query = 'The Beatles - "Hey Jude"'

        await cog_with_player.handle_search_query(interaction, test_query)

        assert query_received == test_query


class TestMusicCogUpdatePlayerMessageRepositioning:
    """Тесты для _update_player_message с reposition параметром."""

    @pytest.fixture
    def cog_with_player(self, mock_config, mock_client, mock_bot):
        """MusicCog с реальным плеером."""
        from bot.player import GuildPlayer

        cog = MusicCog(mock_bot, mock_config, mock_client)
        cog._player = MagicMock(spec=GuildPlayer)
        cog._player.current = None
        cog._player.state = MagicMock()
        cog._replace_player_view = MagicMock()
        cog._delete_player_message = AsyncMock()
        return cog

    @pytest.mark.asyncio
    async def test_update_player_message_reposition_true_deletes_old_when_not_last(
        self, cog_with_player
    ):
        """_update_player_message с reposition=True удаляет старое сообщение при не-last."""
        # Старое сообщение существует
        old_message = MagicMock(spec=discord.Message)
        cog_with_player._player_message = old_message

        # Сообщение не последнее в канале
        cog_with_player._player_message_is_last = MagicMock(return_value=False)

        # Взаимодействие None (вызов из _announce, не из кнопки)
        await cog_with_player._update_player_message(interaction=None, reposition=True)

        # Проверяем, что _delete_player_message была вызвана
        cog_with_player._delete_player_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_player_message_reposition_false_does_not_delete(self, cog_with_player):
        """_update_player_message с reposition=False не удаляет сообщение."""
        # Старое сообщение существует и редактируется
        old_message = MagicMock(spec=discord.Message)
        old_message.edit = AsyncMock()
        cog_with_player._player_message = old_message

        # Сообщение не последнее в канале
        cog_with_player._player_message_is_last = MagicMock(return_value=False)

        # Взаимодействие None
        await cog_with_player._update_player_message(interaction=None, reposition=False)

        # _delete_player_message не должна быть вызвана
        cog_with_player._delete_player_message.assert_not_called()
        # Но старое сообщение должно быть отредактировано
        old_message.edit.assert_called_once()
