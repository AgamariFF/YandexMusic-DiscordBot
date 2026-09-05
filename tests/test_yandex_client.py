"""Tests for bot.yandex.client module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.errors import TrackUnavailableError, WaveUnavailableError
from bot.yandex.client import (
    WAVE_STATION_ID,
    TrackInfo,
    YandexMusicClient,
)


def set_mock_client(client_instance, mock_client):
    """Inject a mock client into YandexMusicClient instance."""
    # Find the private attribute name used by __init__
    instance_vars = vars(client_instance)
    for attr_name in instance_vars:
        if "client" in attr_name.lower() and attr_name.startswith("_"):
            setattr(client_instance, attr_name, mock_client)
            return
    # Fallback
    client_instance._client = mock_client


class TestYandexMusicClientBasics:
    """Basic YandexMusicClient tests."""

    def test_wave_station_id_constant(self):
        """WAVE_STATION_ID equals 'user:onyourwave'."""
        assert WAVE_STATION_ID == "user:onyourwave"

    def test_new_client_defaults(self):
        """New client has correct defaults."""
        client = YandexMusicClient("token123")
        assert client.station == WAVE_STATION_ID
        assert client.connected is False

    def test_custom_station(self):
        """Client with custom station stores it."""
        client = YandexMusicClient("token123", station="custom:station")
        assert client.station == "custom:station"


class TestYandexMusicClientConnect:
    @pytest.mark.asyncio
    async def test_fetch_without_connect_raises_error(self):
        """fetch_wave_batch without connect raises WaveUnavailableError."""
        client = YandexMusicClient("token123")
        # Don't connect - client._client stays None

        with pytest.raises(WaveUnavailableError):
            await client.fetch_wave_batch()


class TestYandexMusicClientFetchBatch:
    """Tests for fetch_wave_batch."""

    @pytest.mark.asyncio
    async def test_fetch_none_result_returns_empty_batch(self):
        """fetch_wave_batch returning None → empty WaveBatch."""
        mock_client = AsyncMock()
        mock_client.rotor_station_tracks = AsyncMock(return_value=None)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        batch = await client.fetch_wave_batch()
        assert batch.batch_id is None
        assert batch.tracks == ()

    @pytest.mark.asyncio
    async def test_fetch_empty_sequence_returns_empty_batch(self):
        """fetch_wave_batch with empty sequence → empty WaveBatch."""
        result = SimpleNamespace(sequence=[], batch_id="batch1")
        mock_client = AsyncMock()
        mock_client.rotor_station_tracks = AsyncMock(return_value=result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        batch = await client.fetch_wave_batch()
        assert batch.batch_id == "batch1"
        assert batch.tracks == ()

    @pytest.mark.asyncio
    async def test_fetch_filters_unavailable_tracks(self):
        """fetch_wave_batch filters out unavailable and None tracks."""
        track1 = SimpleNamespace(
            id="1",
            title="Song1",
            artists=[SimpleNamespace(name="Artist1")],
            duration_ms=180000,
            available=True,
        )
        track2 = None
        track3 = SimpleNamespace(
            id="3",
            title="Song3",
            artists=[SimpleNamespace(name="Artist3A"), SimpleNamespace(name="Artist3B")],
            duration_ms=200000,
            available=False,
        )
        track4 = SimpleNamespace(
            id="4",
            title="Song4",
            artists=[SimpleNamespace(name="Artist4")],
            duration_ms=240000,
            available=True,
        )

        result = SimpleNamespace(
            sequence=[
                SimpleNamespace(track=track1),
                SimpleNamespace(track=track2),
                SimpleNamespace(track=track3),
                SimpleNamespace(track=track4),
            ],
            batch_id="batch1",
        )

        mock_client = AsyncMock()
        mock_client.rotor_station_tracks = AsyncMock(return_value=result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        batch = await client.fetch_wave_batch()

        assert len(batch.tracks) == 2
        assert batch.tracks[0].id == "1"
        assert batch.tracks[0].title == "Song1"
        assert batch.tracks[0].duration == 180.0
        assert batch.tracks[1].id == "4"

    @pytest.mark.asyncio
    async def test_fetch_artists_joined(self):
        """Artists are joined with ', '."""
        track = SimpleNamespace(
            id="1",
            title="Song",
            artists=[
                SimpleNamespace(name="Artist1"),
                SimpleNamespace(name="Artist2"),
            ],
            duration_ms=180000,
            available=True,
        )
        result = SimpleNamespace(
            sequence=[SimpleNamespace(track=track)],
            batch_id="batch1",
        )

        mock_client = AsyncMock()
        mock_client.rotor_station_tracks = AsyncMock(return_value=result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        batch = await client.fetch_wave_batch()

        assert batch.tracks[0].artists == "Artist1, Artist2"

    @pytest.mark.asyncio
    async def test_fetch_empty_artists_fallback(self):
        """Empty artists list → fallback string."""
        track = SimpleNamespace(
            id="1",
            title="Song",
            artists=[],
            duration_ms=180000,
            available=True,
        )
        result = SimpleNamespace(
            sequence=[SimpleNamespace(track=track)],
            batch_id="batch1",
        )

        mock_client = AsyncMock()
        mock_client.rotor_station_tracks = AsyncMock(return_value=result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        batch = await client.fetch_wave_batch()

        assert batch.tracks[0].artists != ""


class TestYandexMusicClientNotify:
    """Tests for notify_* methods."""

    @pytest.mark.asyncio
    async def test_notify_started_no_exception(self):
        """notify_track_started doesn't raise on error."""
        mock_client = AsyncMock()
        mock_client.rotor_station_feedback_track_started = AsyncMock(
            side_effect=Exception("Network error")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise
        await client.notify_track_started("track1", "batch1")

    @pytest.mark.asyncio
    async def test_notify_finished_no_exception(self):
        """notify_track_finished doesn't raise on error."""
        mock_client = AsyncMock()
        mock_client.rotor_station_feedback_track_finished = AsyncMock(
            side_effect=Exception("Network error")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise
        await client.notify_track_finished("track1", 10.0, "batch1")

    @pytest.mark.asyncio
    async def test_notify_skipped_no_exception(self):
        """notify_track_skipped doesn't raise on error."""
        mock_client = AsyncMock()
        mock_client.rotor_station_feedback_skip = AsyncMock(
            side_effect=Exception("Network error")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise
        await client.notify_track_skipped("track1", 10.0, "batch1")


class TestYandexMusicClientResolveStream:
    """Tests for resolve_stream_url."""

    @pytest.mark.asyncio
    async def test_resolve_selects_best_mp3(self):
        """resolve_stream_url selects mp3 with highest bitrate."""
        track_raw = SimpleNamespace()
        info1 = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=128,
            direct="",
            get_direct_link_async=AsyncMock(return_value="url1"),
        )
        info2 = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(return_value="url2"),
        )
        info3 = SimpleNamespace(
            codec="aac",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(return_value="url3"),
        )
        track_raw.get_download_info_async = AsyncMock(return_value=[info1, info2, info3])

        client = YandexMusicClient("token123")
        client._client = MagicMock()  # Mock as connected

        track = TrackInfo(id="1", title="Song", artists="Artist", duration=180.0, raw=track_raw)
        await client.resolve_stream_url(track)

        assert info2.get_direct_link_async.called

    @pytest.mark.asyncio
    async def test_resolve_stream_url_ignores_direct_bool_flag(self):
        """resolve_stream_url ignores direct bool flag and calls get_direct_link_async."""
        track_raw = SimpleNamespace()
        expected_url = "https://actual-link.com/stream"
        info = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct=True,  # bool flag, not a URL string
            get_direct_link_async=AsyncMock(return_value=expected_url),
        )
        track_raw.get_download_info_async = AsyncMock(return_value=[info])

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(id="1", title="Song", artists="Artist", duration=180.0, raw=track_raw)
        url = await client.resolve_stream_url(track)

        # The URL must come from get_direct_link_async, not from direct field
        assert url == expected_url
        assert url is not True
        assert isinstance(url, str)
        assert info.get_direct_link_async.called

    @pytest.mark.asyncio
    async def test_resolve_empty_list_raises_error(self):
        """resolve_stream_url with empty download_info raises TrackUnavailableError."""
        track_raw = SimpleNamespace()
        track_raw.get_download_info_async = AsyncMock(return_value=[])

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(id="1", title="Song", artists="Artist", duration=180.0, raw=track_raw)

        with pytest.raises(TrackUnavailableError):
            await client.resolve_stream_url(track)

    @pytest.mark.asyncio
    async def test_resolve_only_preview_raises_error(self):
        """resolve_stream_url with only preview variants raises TrackUnavailableError."""
        track_raw = SimpleNamespace()
        info = SimpleNamespace(
            codec="mp3",
            preview=True,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(),
        )
        track_raw.get_download_info_async = AsyncMock(return_value=[info])

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(id="1", title="Song", artists="Artist", duration=180.0, raw=track_raw)

        with pytest.raises(TrackUnavailableError):
            await client.resolve_stream_url(track)


class TestTrackInfoDisplay:
    """Tests for TrackInfo.display property."""

    def test_display_with_positive_duration(self):
        """display includes artist and title with duration."""
        track = TrackInfo(
            id="1",
            title="Song Title",
            artists="Artist Name",
            duration=225.0,  # 3:45
            raw=None,
        )
        display = track.display
        assert "Artist Name" in display
        assert "Song Title" in display
        assert "[" in display and "]" in display

    def test_display_with_zero_duration(self):
        """display without brackets when duration <= 0."""
        track = TrackInfo(
            id="1",
            title="Song Title",
            artists="Artist Name",
            duration=0.0,
            raw=None,
        )
        display = track.display
        assert "Artist Name" in display
        assert "Song Title" in display
        assert "[" not in display


class TestYandexMusicClientClose:
    """Tests for close method."""

    @pytest.mark.asyncio
    async def test_close_idempotent(self):
        """close() can be called multiple times without error."""
        client = YandexMusicClient("token123")
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        set_mock_client(client, mock_client)

        # First close
        await client.close()
        assert client.connected is False

        # Second close should not raise
        await client.close()
        assert client.connected is False
