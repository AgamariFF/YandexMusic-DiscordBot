"""Tests for bot.player module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.audio.bassboost import BassLevel
from bot.errors import BotError, NotConnectedError, NothingPlayingError
from bot.player import GuildPlayer, PlayerState
from bot.yandex.client import TrackInfo


@pytest.fixture
def fake_client():
    """Fake YandexMusicClient for testing."""
    return AsyncMock()


@pytest.fixture
def player(fake_client):
    """Create a GuildPlayer with fake client."""
    return GuildPlayer(
        fake_client,
        ffmpeg_path="ffmpeg",
        default_volume=0.5,
        idle_timeout=300,
    )


class TestPlayerInitialState:
    """Tests for initial player state."""

    def test_initial_state_idle(self, player):
        """New player state is IDLE."""
        assert player.state == PlayerState.IDLE

    def test_initial_current_none(self, player):
        """New player current is None."""
        assert player.current is None

    def test_initial_voice_client_none(self, player):
        """New player voice_client is None."""
        assert player.voice_client is None

    def test_initial_channel_none(self, player):
        """New player channel is None."""
        assert player.channel is None

    def test_initial_volume(self, player):
        """New player volume matches default."""
        assert player.volume == 0.5

    def test_initial_bass_off(self, player):
        """New player bass is OFF."""
        assert player.bass == BassLevel.OFF


class TestPlayerNowPlaying:
    """Tests for now_playing() method."""

    def test_now_playing_without_track_raises_error(self, player):
        """now_playing() without active track raises NothingPlayingError."""
        with pytest.raises(NothingPlayingError):
            player.now_playing()


class TestPlayerPauseResume:
    """Tests for pause() and resume() methods."""

    def test_pause_without_connection_raises_error(self, player):
        """pause() without connection raises NotConnectedError."""
        with pytest.raises(NotConnectedError):
            player.pause()

    def test_resume_without_connection_raises_error(self, player):
        """resume() without connection raises NotConnectedError."""
        with pytest.raises(NotConnectedError):
            player.resume()

    def test_pause_without_track_raises_error(self, player):
        """pause() without active track raises NothingPlayingError."""
        player._voice_client = MagicMock()
        player._voice_client.is_playing.return_value = False

        with pytest.raises(NothingPlayingError):
            player.pause()

    def test_resume_without_track_raises_error(self, player):
        """resume() without active track raises NothingPlayingError."""
        player._voice_client = MagicMock()
        player._voice_client.is_paused.return_value = False

        with pytest.raises(NothingPlayingError):
            player.resume()


class TestPlayerSkip:
    """Tests for skip() method."""

    @pytest.mark.asyncio
    async def test_skip_without_connection_raises_error(self, player):
        """skip() without connection raises BotError."""
        with pytest.raises(BotError):
            await player.skip()

    @pytest.mark.asyncio
    async def test_skip_without_track_raises_error(self, player):
        """skip() without active track raises BotError."""
        player._voice_client = MagicMock()
        player._voice_client.is_playing.return_value = False
        player._voice_client.is_paused.return_value = False

        with pytest.raises(BotError):
            await player.skip()


class TestPlayerStartWave:
    """Tests for start_wave() method."""

    @pytest.mark.asyncio
    async def test_start_wave_without_connection_raises_error(self, player):
        """start_wave() without connection raises NotConnectedError."""
        with pytest.raises(NotConnectedError):
            await player.start_wave()


class TestPlayerVolume:
    """Tests for volume control."""

    def test_set_volume_clamps_high(self, player):
        """set_volume(5.0) clamps to 2.0."""
        result = player.set_volume(5.0)
        assert result == 2.0
        assert player.volume == 2.0

    def test_set_volume_clamps_low(self, player):
        """set_volume(-1.0) clamps to 0.0."""
        result = player.set_volume(-1.0)
        assert result == 0.0
        assert player.volume == 0.0

    def test_set_volume_valid(self, player):
        """set_volume(0.75) returns and stores 0.75."""
        result = player.set_volume(0.75)
        assert result == 0.75
        assert player.volume == 0.75


class TestPlayerBass:
    """Tests for bass control."""

    @pytest.mark.asyncio
    async def test_set_bass_without_track(self, player):
        """set_bass() without track doesn't raise."""
        await player.set_bass(BassLevel.HIGH)
        assert player.bass == BassLevel.HIGH


class TestPlayerQueuePreview:
    """Tests for queue_preview() method."""

    def test_queue_preview_without_wave_empty(self, player):
        """queue_preview() without wave returns empty list."""
        result = player.queue_preview()
        assert result == []

    def test_queue_preview_with_session(self, player):
        """queue_preview() delegates to session."""
        mock_session = MagicMock()
        track1 = TrackInfo(id="1", title="Song1", artists="Artist", duration=180.0, raw=None)
        mock_session.upcoming.return_value = [track1]

        player._session = mock_session

        result = player.queue_preview(5)

        assert result == [track1]
        mock_session.upcoming.assert_called_once_with(5)


class TestPlayerDisconnect:
    """Tests for disconnect() method."""

    @pytest.mark.asyncio
    async def test_disconnect_without_connection(self, player):
        """disconnect() without connection doesn't raise."""
        await player.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self, player):
        """disconnect() called twice doesn't raise."""
        await player.disconnect()
        await player.disconnect()


class TestPlaybackCallbackIdentity:
    """Regression tests for playback callback identity check (race condition fix)."""

    @pytest.mark.asyncio
    async def test_outdated_source_callback_no_side_effects(self, fake_client):
        """Playback callback with outdated source does not produce side effects.

        This is a regression test for the race condition where two consecutive
        stop operations would cause the first callback to consume the flag meant
        for both, and the second callback would incorrectly advance the queue.

        The fix: _handle_playback_finished checks if source is not self._source
        and returns early without side effects.
        """
        player = GuildPlayer(fake_client, idle_timeout=0)

        # Create fake wave session with tracking for method calls
        fake_session = MagicMock()
        fake_session.track_finished = AsyncMock()
        fake_session.next_track = AsyncMock()
        fake_session.track_started = AsyncMock()

        # Set up player with a current track and source
        player._session = fake_session
        current_track = TrackInfo(
            id="1", title="Current", artists="Artist", duration=100.0, raw=None
        )
        current_source = MagicMock()  # The actual current source
        current_source.elapsed = 50.0
        player._current_track = current_track
        player._source = current_source
        player._state = PlayerState.PLAYING

        # Create an outdated source object (from a previous playback)
        outdated_source = MagicMock()
        outdated_source.elapsed = 0.0

        # Call _handle_playback_finished with the OUTDATED source
        # This simulates a callback arriving after the source has been replaced
        await player._handle_playback_finished(outdated_source)

        # Verify no side effects occurred:
        # - track_finished should NOT be called
        fake_session.track_finished.assert_not_called()
        # - next_track should NOT be called
        fake_session.next_track.assert_not_called()
        # - current track should remain unchanged
        assert player._current_track is current_track
        # - current source should remain unchanged
        assert player._source is current_source
