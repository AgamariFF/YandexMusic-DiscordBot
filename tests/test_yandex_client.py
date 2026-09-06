"""Tests for bot.yandex.client module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yandex_music.exceptions

from bot.errors import TrackUnavailableError, WaveUnavailableError, YandexAuthError
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
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.get = AsyncMock(return_value=None)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch(
            "bot.yandex.client.StationTracksResult.de_json",
            return_value=SimpleNamespace(sequence=[], batch_id=None),
        ):
            batch = await client.fetch_wave_batch()
        assert batch.batch_id is None
        assert batch.tracks == ()

    @pytest.mark.asyncio
    async def test_fetch_empty_sequence_returns_empty_batch(self):
        """fetch_wave_batch with empty sequence → empty WaveBatch."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.get = AsyncMock(return_value={})

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch(
            "bot.yandex.client.StationTracksResult.de_json",
            return_value=SimpleNamespace(sequence=[], batch_id="batch1"),
        ):
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

        result_tracks = SimpleNamespace(
            sequence=[
                SimpleNamespace(track=track1),
                SimpleNamespace(track=track2),
                SimpleNamespace(track=track3),
                SimpleNamespace(track=track4),
            ],
            batch_id="batch1",
        )

        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.get = AsyncMock(return_value={})

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch(
            "bot.yandex.client.StationTracksResult.de_json",
            return_value=result_tracks,
        ):
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

        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.get = AsyncMock(return_value={})

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch(
            "bot.yandex.client.StationTracksResult.de_json", return_value=result
        ):
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

        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.get = AsyncMock(return_value={})

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch(
            "bot.yandex.client.StationTracksResult.de_json", return_value=result
        ):
            batch = await client.fetch_wave_batch()

        assert batch.tracks[0].artists != ""


class TestYandexMusicClientNotify:
    """Tests for notify_* methods."""

    @pytest.mark.asyncio
    async def test_notify_started_no_exception(self):
        """notify_track_started doesn't raise on error."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(side_effect=Exception("Network error"))

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise
        await client.notify_track_started("track1", "batch1")

    @pytest.mark.asyncio
    async def test_notify_finished_no_exception(self):
        """notify_track_finished doesn't raise on error."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(side_effect=Exception("Network error"))

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise
        await client.notify_track_finished("track1", 10.0, "batch1")

    @pytest.mark.asyncio
    async def test_notify_skipped_no_exception(self):
        """notify_track_skipped doesn't raise on error."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(side_effect=Exception("Network error"))

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


class TestYandexMusicClientStartWave:
    """Tests for start_wave method."""

    @pytest.mark.asyncio
    async def test_start_wave_survives_feedback_rejection(self):
        """Server rejects feedback with BadRequestError, but wave still starts.

        Regression test for a production bug: when the Yandex API rejects
        rotor_station_feedback_radio_started with BadRequestError (condition
        is not met), the wave is still playable, so start_wave() should NOT
        raise an exception—only log a warning. Wave functionality must not
        depend on optional feedback success.
        """
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(
            side_effect=yandex_music.exceptions.BadRequestError("condition is not met")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise despite BadRequestError from feedback
        await client.start_wave()

    @pytest.mark.asyncio
    async def test_start_wave_expired_token_still_raises_auth_error(self):
        """An expired token stays fatal even though feedback failures are tolerated.

        Otherwise the user would see "wave unavailable" and never learn that
        the token must be reissued.
        """
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(
            side_effect=yandex_music.exceptions.UnauthorizedError("token expired")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(YandexAuthError):
            await client.start_wave()

    @pytest.mark.asyncio
    async def test_start_wave_survives_network_error(self):
        """Network error in feedback does not stop wave startup."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(
            side_effect=yandex_music.exceptions.NetworkError("Connection timeout")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise despite NetworkError from feedback
        await client.start_wave()

    @pytest.mark.asyncio
    async def test_start_wave_survives_generic_exception(self):
        """Generic Exception in feedback does not stop wave startup."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock(side_effect=Exception("Some error"))

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Should not raise despite generic exception
        await client.start_wave()

    @pytest.mark.asyncio
    async def test_start_wave_without_client_raises_error(self):
        """start_wave without connect raises WaveUnavailableError.

        Unlike feedback errors, the absence of a client is a state error
        that must bubble up.
        """
        client = YandexMusicClient("token123")
        # Don't connect - client._client stays None

        with pytest.raises(WaveUnavailableError):
            await client.start_wave()

    @pytest.mark.asyncio
    async def test_start_wave_success(self):
        """Successful feedback: start_wave does not raise and calls _request.post."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock()

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        await client.start_wave()

        # Verify _request.post was called exactly once
        assert mock_client._request.post.call_count == 1
        # Check the URL contains the wave station ID
        call_args = mock_client._request.post.call_args
        url = call_args[0][0]
        assert url.endswith(f"/rotor/station/{WAVE_STATION_ID}/feedback")
        # Check json payload contains radioStarted type
        assert call_args[1]["json"]["type"] == "radioStarted"

    @pytest.mark.asyncio
    async def test_start_wave_with_custom_params(self):
        """start_wave passes from_ and batch_id to _request.post."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock()

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        await client.start_wave(from_="custom_from", batch_id="custom_batch_id")

        # Verify _request.post was called with correct parameters
        call_args = mock_client._request.post.call_args
        # Check json payload contains custom from and correct type
        assert call_args[1]["json"]["from"] == "custom_from"
        assert call_args[1]["json"]["type"] == "radioStarted"
        # Check params contain batch-id
        assert call_args[1]["params"]["batch-id"] == "custom_batch_id"


class TestFeedbackRegression:
    """Regression tests for feedback implementation details."""

    @pytest.mark.asyncio
    async def test_feedback_sent_as_json_not_form(self):
        """Feedback must be sent as JSON, not form data.

        Regression test for a production bug where the old implementation
        sent form-encoded feedback and received 400 errors from the server.
        Now we send json as a named argument, not data=form.
        """
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock()

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        await client.notify_track_started("track1", "batch1")

        # Verify _request.post was called with json argument, not data
        call_args = mock_client._request.post.call_args
        assert "json" in call_args[1]
        assert "data" not in call_args[1]

    @pytest.mark.asyncio
    async def test_feedback_payload_fields_per_type(self):
        """Each feedback type has correct required and optional fields."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock()

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Test radioStarted: has 'from', no trackId
        await client.start_wave(from_="test_from")
        call_args = mock_client._request.post.call_args
        payload = call_args[1]["json"]
        assert payload["type"] == "radioStarted"
        assert "timestamp" in payload
        assert payload["from"] == "test_from"
        assert "trackId" not in payload

        # Test trackStarted: has trackId, no totalPlayedSeconds
        mock_client._request.post.reset_mock()
        await client.notify_track_started("track1", "batch1")
        call_args = mock_client._request.post.call_args
        payload = call_args[1]["json"]
        assert payload["type"] == "trackStarted"
        assert "timestamp" in payload
        assert payload["trackId"] == "track1"
        assert "totalPlayedSeconds" not in payload

        # Test trackFinished: has trackId and totalPlayedSeconds
        mock_client._request.post.reset_mock()
        await client.notify_track_finished("track1", 42.5, "batch1")
        call_args = mock_client._request.post.call_args
        payload = call_args[1]["json"]
        assert payload["type"] == "trackFinished"
        assert "timestamp" in payload
        assert payload["trackId"] == "track1"
        assert payload["totalPlayedSeconds"] == 42.5

        # Test skip: has trackId and totalPlayedSeconds
        mock_client._request.post.reset_mock()
        await client.notify_track_skipped("track1", 10.0, "batch1")
        call_args = mock_client._request.post.call_args
        payload = call_args[1]["json"]
        assert payload["type"] == "skip"
        assert "timestamp" in payload
        assert payload["trackId"] == "track1"
        assert payload["totalPlayedSeconds"] == 10.0

    @pytest.mark.asyncio
    async def test_skip_reports_played_seconds(self):
        """skip feedback includes totalPlayedSeconds."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock()

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        await client.notify_track_skipped("t1", 12.5, "b1")

        call_args = mock_client._request.post.call_args
        payload = call_args[1]["json"]
        assert payload["totalPlayedSeconds"] == 12.5
        assert payload["type"] == "skip"

    @pytest.mark.asyncio
    async def test_fetch_always_sends_settings2(self):
        """fetch_wave_batch always sends settings2 param.

        Regression test: old library method overwrote params dict and lost
        settings2 when a queue param was provided. New implementation must
        always include settings2 in params.
        """
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.get = AsyncMock(return_value={})

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        # Test without queue
        with patch(
            "bot.yandex.client.StationTracksResult.de_json",
            return_value=SimpleNamespace(sequence=[], batch_id="batch1"),
        ):
            await client.fetch_wave_batch()

        call_args = mock_client._request.get.call_args
        # get is called positionally: await client._request.get(url, params)
        params = call_args[0][1]
        assert params["settings2"] == "True"
        assert "queue" not in params

        # Test with queue
        mock_client._request.get.reset_mock()
        with patch(
            "bot.yandex.client.StationTracksResult.de_json",
            return_value=SimpleNamespace(sequence=[], batch_id="batch2"),
        ):
            await client.fetch_wave_batch(queue="queue123")

        call_args = mock_client._request.get.call_args
        # get is called positionally: await client._request.get(url, params)
        params = call_args[0][1]
        assert params["settings2"] == "True"
        assert params["queue"] == "queue123"

    @pytest.mark.asyncio
    async def test_zero_played_seconds_still_sent(self):
        """totalPlayedSeconds=0.0 is still included in payload.

        Regression test: code must not filter out 0 as falsy value.
        """
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client._request = MagicMock()
        mock_client._request.post = AsyncMock()

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        await client.notify_track_finished("t1", 0.0, "b1")

        call_args = mock_client._request.post.call_args
        payload = call_args[1]["json"]
        assert "totalPlayedSeconds" in payload
        assert payload["totalPlayedSeconds"] == 0.0
