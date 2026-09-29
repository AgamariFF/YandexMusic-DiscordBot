"""Tests for bot.yandex.client module."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yandex_music.exceptions

from bot.errors import (
    SearchUnavailableError,
    TrackUnavailableError,
    WaveUnavailableError,
    YandexAuthError,
)
from bot.yandex.client import (
    WAVE_STATION_ID,
    TrackInfo,
    YandexMusicClient,
    build_feedback,
    build_radio_started_feedback,
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


def create_mock_track(
    track_id,
    title,
    artists,
    duration_ms=180000,
    available=True,
    album_id=None,
    cover_uri=None,
    og_image=None,
):
    """Создаёт мок трека с необходимыми полями для cover_url.

    Возвращает объект SimpleNamespace с полями cover_uri, og_image и методами
    get_cover_url, get_og_image_url.
    """
    # Создаём методы для получения обложки
    def get_cover_url(size):
        if cover_uri:
            return f"{cover_uri.replace('%%', size)}"
        return None

    def get_og_image_url(size):
        if og_image:
            return f"{og_image.replace('%%', size)}"
        return None

    # Если artists - строка, оборачиваем в список
    if isinstance(artists, str):
        artist_objects = [SimpleNamespace(name=artists)]
    else:
        artist_objects = artists

    track = SimpleNamespace(
        id=track_id,
        title=title,
        artists=artist_objects,
        duration_ms=duration_ms,
        available=available,
        albums=[SimpleNamespace(id=album_id)] if album_id else [],
        cover_uri=cover_uri,
        og_image=og_image,
    )

    # Добавляем методы
    track.get_cover_url = get_cover_url
    track.get_og_image_url = get_og_image_url

    return track


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
    async def test_start_session_without_connect_raises_error(self):
        """start_session() без connect() выбрасывает WaveUnavailableError."""
        client = YandexMusicClient("token123")

        with pytest.raises(WaveUnavailableError):
            await client.start_session()

    @pytest.mark.asyncio
    async def test_fetch_session_tracks_without_connect_raises_error(self):
        """fetch_session_tracks() без connect() выбрасывает WaveUnavailableError."""
        client = YandexMusicClient("token123")

        with pytest.raises(WaveUnavailableError):
            await client.fetch_session_tracks(queue=[], feedbacks=[])


class TestYandexMusicClientFetchSessionTracks:
    """Тесты для fetch_session_tracks()."""

    @pytest.mark.asyncio
    async def test_fetch_none_result_returns_empty_batch(self):
        """fetch_session_tracks с None → пустой WaveBatch."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=None)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        batch = await client.fetch_session_tracks(queue=[], feedbacks=[])
        assert batch.batch_id is None
        assert batch.tracks == ()

    @pytest.mark.asyncio
    async def test_fetch_empty_sequence_returns_empty_batch(self):
        """fetch_session_tracks с пустой sequence → пустой WaveBatch."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        raw = {"batchId": "batch1", "sequence": []}
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        batch = await client.fetch_session_tracks(queue=[], feedbacks=[])
        assert batch.batch_id == "batch1"
        assert batch.tracks == ()

    @pytest.mark.asyncio
    async def test_fetch_filters_unavailable_tracks(self):
        """fetch_session_tracks фильтрует недоступные и элементы без track."""
        track1 = create_mock_track("1", "Song1", "Artist1", 180000, True, "101")
        track3 = create_mock_track(
            "3",
            "Song3",
            [SimpleNamespace(name="Artist3A"), SimpleNamespace(name="Artist3B")],
            200000,
            False,
            "103",
        )
        track4 = create_mock_track("4", "Song4", "Artist4", 240000, True, "104")

        raw = {
            "batchId": "batch1",
            "sequence": [
                {"track": track1},
                {},  # элемент без track
                {"track": track3},
                {"track": track4},
            ],
        }

        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        with patch("bot.yandex.client.Track.de_json") as track_de_json:
            track_de_json.side_effect = [track1, track3, track4]
            batch = await client.fetch_session_tracks(queue=[], feedbacks=[])

        assert len(batch.tracks) == 2
        assert batch.tracks[0].id == "1"
        assert batch.tracks[0].title == "Song1"
        assert batch.tracks[0].duration == 180.0
        assert batch.tracks[1].id == "4"

    @pytest.mark.asyncio
    async def test_fetch_artists_joined(self):
        """Артисты объединяются с ', '."""
        track = create_mock_track(
            "1",
            "Song",
            [SimpleNamespace(name="Artist1"), SimpleNamespace(name="Artist2")],
            180000,
            True,
            "101",
        )
        raw = {
            "batchId": "batch1",
            "sequence": [{"track": track}],
        }

        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        with patch("bot.yandex.client.Track.de_json", return_value=track):
            batch = await client.fetch_session_tracks(queue=[], feedbacks=[])

        assert batch.tracks[0].artists == "Artist1, Artist2"

    @pytest.mark.asyncio
    async def test_fetch_empty_artists_fallback(self):
        """Пустой список артистов → запасная строка."""
        track = create_mock_track("1", "Song", [], 180000, True, "101")
        raw = {
            "batchId": "batch1",
            "sequence": [{"track": track}],
        }

        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        with patch("bot.yandex.client.Track.de_json", return_value=track):
            batch = await client.fetch_session_tracks(queue=[], feedbacks=[])

        assert batch.tracks[0].artists != ""


class TestYandexMusicClientSendFeedbacks:
    """Тесты для send_feedbacks()."""

    @pytest.mark.asyncio
    async def test_send_feedbacks_no_exception_on_error(self):
        """send_feedbacks() не выбрасывает исключение при ошибке."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(side_effect=Exception("Network error"))

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        # Не должна выбросить исключение
        await client.send_feedbacks([{"event": {"type": "skip"}}])

    @pytest.mark.asyncio
    async def test_send_empty_feedbacks_no_request(self):
        """send_feedbacks([]) не делает запроса."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock()

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        await client.send_feedbacks([])

        assert not mock_client.request.post.called

    @pytest.mark.asyncio
    async def test_send_feedbacks_path_ends_with_slash(self):
        """send_feedbacks() использует путь с завершающим слэшем."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock()

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        await client.send_feedbacks([{"event": {"type": "skip"}}])

        call_args = mock_client.request.post.call_args
        url = call_args[0][0]
        assert url.endswith("/feedbacks/"), (
            f"URL должен заканчиваться на '/feedbacks/', получено {url}"
        )

    @pytest.mark.asyncio
    async def test_all_methods_use_json_not_data(self):
        """Все методы передают тело как json=, а не data=.

        Регрессионный тест: старая реализация отправляла как form-encoded,
        сервер отвечал 400. Тело должно идти именованным аргументом json=.
        """
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(
            return_value={"radioSessionId": "sid", "batchId": "b1", "sequence": []}
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        await client.start_session()
        call_args = mock_client.request.post.call_args
        assert "json" in call_args[1], "start_session должен использовать json="
        assert "data" not in call_args[1], "start_session не должен использовать data="

        mock_client.request.post.reset_mock()
        mock_client.request.post.return_value = {"batchId": "b2", "sequence": []}
        client._radio_session_id = "session123"

        await client.fetch_session_tracks(queue=[], feedbacks=[])
        call_args = mock_client.request.post.call_args
        assert "json" in call_args[1], "fetch_session_tracks должен использовать json="
        assert "data" not in call_args[1], "fetch_session_tracks не должен использовать data="

        mock_client.request.post.reset_mock()
        await client.send_feedbacks([{"event": {"type": "skip"}}])
        call_args = mock_client.request.post.call_args
        assert "json" in call_args[1], "send_feedbacks должен использовать json="
        assert "data" not in call_args[1], "send_feedbacks не должен использовать data="


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

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )
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

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )
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

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

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

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

        with pytest.raises(TrackUnavailableError):
            await client.resolve_stream_url(track)


class TestYandexMusicClientResolveStreamRetries:
    """Tests for resolve_stream_url retry behavior."""

    @pytest.mark.asyncio
    async def test_first_call_retries_on_yandex_music_error_and_succeeds(
        self, monkeypatch
    ):
        """get_download_info_async fails twice, succeeds on third attempt."""
        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        track_raw = SimpleNamespace()
        info = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(return_value="https://stream.url"),
        )
        track_raw.get_download_info_async = AsyncMock(
            side_effect=[
                yandex_music.exceptions.YandexMusicError("error1"),
                yandex_music.exceptions.YandexMusicError("error2"),
                [info],
            ]
        )

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )
        url = await client.resolve_stream_url(track)

        assert url == "https://stream.url"
        assert track_raw.get_download_info_async.call_count == 3

    @pytest.mark.asyncio
    async def test_first_call_exhausts_retries_raises_track_unavailable(
        self, monkeypatch
    ):
        """get_download_info_async fails all MAX_ATTEMPTS times."""
        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        track_raw = SimpleNamespace()
        track_raw.get_download_info_async = AsyncMock(
            side_effect=yandex_music.exceptions.YandexMusicError("persistent error")
        )

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

        with pytest.raises(TrackUnavailableError):
            await client.resolve_stream_url(track)

        assert (
            track_raw.get_download_info_async.call_count
            == YandexMusicClient.STREAM_URL_MAX_ATTEMPTS
        )

    @pytest.mark.asyncio
    async def test_first_call_unauthorized_error_no_retry(self):
        """get_download_info_async raises UnauthorizedError → YandexAuthError,
        no retries.
        """
        track_raw = SimpleNamespace()
        track_raw.get_download_info_async = AsyncMock(
            side_effect=yandex_music.exceptions.UnauthorizedError("invalid token")
        )

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

        with pytest.raises(YandexAuthError):
            await client.resolve_stream_url(track)

        assert track_raw.get_download_info_async.call_count == 1

    @pytest.mark.asyncio
    async def test_second_call_retries_on_yandex_music_error_and_succeeds(
        self, monkeypatch
    ):
        """get_direct_link_async fails once, succeeds on second attempt."""
        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        track_raw = SimpleNamespace()
        info = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(
                side_effect=[
                    yandex_music.exceptions.YandexMusicError("network error"),
                    "https://direct.url",
                ]
            ),
        )
        track_raw.get_download_info_async = AsyncMock(return_value=[info])

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )
        url = await client.resolve_stream_url(track)

        assert url == "https://direct.url"
        assert info.get_direct_link_async.call_count == 2

    @pytest.mark.asyncio
    async def test_second_call_exhausts_retries_raises_track_unavailable(
        self, monkeypatch
    ):
        """get_direct_link_async fails all MAX_ATTEMPTS times."""
        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        track_raw = SimpleNamespace()
        info = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(
                side_effect=yandex_music.exceptions.YandexMusicError("persistent error")
            ),
        )
        track_raw.get_download_info_async = AsyncMock(return_value=[info])

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

        with pytest.raises(TrackUnavailableError):
            await client.resolve_stream_url(track)

        assert (
            info.get_direct_link_async.call_count
            == YandexMusicClient.STREAM_URL_MAX_ATTEMPTS
        )

    @pytest.mark.asyncio
    async def test_second_call_unauthorized_error_no_retry(self):
        """get_direct_link_async raises UnauthorizedError → YandexAuthError,
        no retries.
        """
        track_raw = SimpleNamespace()
        info = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(
                side_effect=yandex_music.exceptions.UnauthorizedError("invalid token")
            ),
        )
        track_raw.get_download_info_async = AsyncMock(return_value=[info])

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

        with pytest.raises(YandexAuthError):
            await client.resolve_stream_url(track)

        assert info.get_direct_link_async.call_count == 1

    @pytest.mark.asyncio
    async def test_retry_delays_grow_exponentially(self, monkeypatch):
        """Retry delays follow formula: RETRY_DELAY_SECONDS * 2^(attempt-1).
        After last failed attempt, no sleep occurs.
        """
        delays: list[float] = []

        async def recording_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr("asyncio.sleep", recording_sleep)

        track_raw = SimpleNamespace()
        track_raw.get_download_info_async = AsyncMock(
            side_effect=yandex_music.exceptions.YandexMusicError("error")
        )

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )

        with pytest.raises(TrackUnavailableError):
            await client.resolve_stream_url(track)

        # Expected delays: 0.5 * 2^0 = 0.5, 0.5 * 2^1 = 1.0
        # After 3rd attempt (last), no sleep
        expected_delays = [
            YandexMusicClient.STREAM_URL_RETRY_DELAY_SECONDS * (2 ** i)
            for i in range(YandexMusicClient.STREAM_URL_MAX_ATTEMPTS - 1)
        ]
        assert delays == expected_delays
        assert len(delays) == YandexMusicClient.STREAM_URL_MAX_ATTEMPTS - 1

    @pytest.mark.asyncio
    async def test_os_error_retries_like_yandex_music_error(self, monkeypatch):
        """OSError is retried same as YandexMusicError."""
        async def fake_sleep(seconds):
            pass

        monkeypatch.setattr("asyncio.sleep", fake_sleep)

        track_raw = SimpleNamespace()
        info = SimpleNamespace(
            codec="mp3",
            preview=False,
            bitrate_in_kbps=320,
            direct="",
            get_direct_link_async=AsyncMock(return_value="https://stream.url"),
        )
        track_raw.get_download_info_async = AsyncMock(
            side_effect=[
                OSError("connection failed"),
                [info],
            ]
        )

        client = YandexMusicClient("token123")
        client._client = MagicMock()

        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song",
            artists="Artist",
            duration=180.0,
            cover_url="https://example.com/cover.jpg",
            raw=track_raw,
        )
        url = await client.resolve_stream_url(track)

        assert url == "https://stream.url"
        assert track_raw.get_download_info_async.call_count == 2


class TestTrackInfoDisplay:
    """Tests for TrackInfo.display property."""

    def test_display_with_positive_duration(self):
        """display includes artist and title with duration."""
        track = TrackInfo(
            id="1",
            feedback_id="1:1",
            title="Song Title",
            artists="Artist Name",
            duration=225.0,  # 3:45
            cover_url="https://example.com/cover.jpg",
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
            feedback_id="1:1",
            title="Song Title",
            artists="Artist Name",
            duration=0.0,
            cover_url="https://example.com/cover.jpg",
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


class TestYandexMusicClientStartSession:
    """Тесты для start_session()."""

    @pytest.mark.asyncio
    async def test_start_session_success(self):
        """Успешное создание сессии возвращает WaveBatch."""
        raw = {
            "radioSessionId": "session123",
            "batchId": "batch1",
            "sequence": [],
        }
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        batch = await client.start_session()

        assert batch.batch_id == "batch1"
        assert batch.tracks == ()
        assert client._radio_session_id == "session123"

    @pytest.mark.asyncio
    async def test_start_session_no_radio_session_id_raises_error(self):
        """Если ответ не содержит radioSessionId → WaveUnavailableError."""
        raw = {"batchId": "batch1", "sequence": []}
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(WaveUnavailableError):
            await client.start_session()

    @pytest.mark.asyncio
    async def test_start_session_expired_token_raises_auth_error(self):
        """Истёкший токен выбрасывает YandexAuthError."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(
            side_effect=yandex_music.exceptions.UnauthorizedError("token expired")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(YandexAuthError):
            await client.start_session()

    @pytest.mark.asyncio
    async def test_fetch_session_tracks_without_session_raises_error(self):
        """fetch_session_tracks без start_session() выбрасывает WaveUnavailableError."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(WaveUnavailableError):
            await client.fetch_session_tracks(queue=[], feedbacks=[])


class TestBuildFeedback:
    """Тесты для функции build_feedback()."""

    def test_build_radio_started_feedback(self):
        """build_radio_started_feedback имеет правильную структуру: from дублируется.

        Это основной случай защиты от регрессии: ревьюер нашёл мутацией, что
        убрание "from" из внутреннего event не ловилось тестами. Теперь проверяем
        что from присутствует и внутри event, и на верхнем уровне; batchId отсутствует;
        timestamp в формате ISO с суффиксом Z; тип события — radioStarted.
        """
        feedback = build_radio_started_feedback(from_="custom_from")

        # Проверяем структуру: есть event и from на верхнем уровне
        assert "event" in feedback
        assert "from" in feedback
        assert feedback["from"] == "custom_from"

        # В event дублируется from и timestamp
        event = feedback["event"]
        assert event["type"] == "radioStarted"
        assert event["from"] == "custom_from", (
            "Поле 'from' должно присутствовать внутри event (дублироваться)"
        )

        # Проверяем формат timestamp: ISO-8601 с суффиксом Z, без +00:00
        ts = event["timestamp"]
        assert ts.endswith("Z"), f"Timestamp должен заканчиваться на Z, получено {ts}"
        assert "+00:00" not in ts, f"Timestamp не должен содержать +00:00, получено {ts}"

        # radioStarted НЕ имеет batchId (пачка ещё не запрашивалась)
        assert "batchId" not in feedback, (
            "radioStarted не должен иметь batchId на верхнем уровне"
        )
        # radioStarted НЕ имеет trackId (это инициирующий событие, не на трек)
        assert "trackId" not in event

    def test_build_feedback_track_started(self):
        """build_feedback для trackStarted имеет trackId и from."""
        feedback = build_feedback("trackStarted", batch_id="batch1", track_id="track123")

        assert feedback["event"]["type"] == "trackStarted"
        # trackId должен быть составным (feedback_id), не голым id
        assert feedback["event"]["trackId"] == "track123"
        assert feedback["batchId"] == "batch1"
        # Проверяем что from всегда присутствует
        assert "from" in feedback, "Поле 'from' должно быть в каждом фидбеке"
        # Проверяем формат timestamp
        ts = feedback["event"]["timestamp"]
        assert ts.endswith("Z"), f"Timestamp должен заканчиваться на Z, получено {ts}"
        assert "totalPlayedSeconds" not in feedback["event"]

    def test_build_feedback_track_finished(self):
        """build_feedback для trackFinished имеет totalPlayedSeconds и trackLengthSeconds."""
        feedback = build_feedback(
            "trackFinished",
            batch_id="batch1",
            track_id="track123",
            total_played_seconds=42.5,
            track_length_seconds=180.0,
        )

        assert feedback["event"]["type"] == "trackFinished"
        assert feedback["event"]["trackId"] == "track123"
        assert feedback["event"]["totalPlayedSeconds"] == 42.5
        assert feedback["event"]["trackLengthSeconds"] == 180.0
        assert feedback["batchId"] == "batch1"

    def test_build_feedback_skip(self):
        """build_feedback для skip имеет trackId и totalPlayedSeconds."""
        feedback = build_feedback(
            "skip",
            batch_id="batch1",
            track_id="track123",
            total_played_seconds=10.0,
        )

        assert feedback["event"]["type"] == "skip"
        assert feedback["event"]["trackId"] == "track123"
        assert feedback["event"]["totalPlayedSeconds"] == 10.0
        assert feedback["batchId"] == "batch1"

    def test_build_feedback_none_values_not_included(self):
        """build_feedback не включает ключи со значением None."""
        feedback = build_feedback(
            "trackStarted",
            batch_id=None,
            track_id="track1",
            total_played_seconds=None,
        )

        assert "batchId" not in feedback
        assert "totalPlayedSeconds" not in feedback["event"]
        assert feedback["event"]["trackId"] == "track1"

    def test_build_feedback_zero_still_included(self):
        """build_feedback включает 0.0 (не фильтрует как falsy)."""
        feedback = build_feedback(
            "trackFinished",
            batch_id="b1",
            track_id="t1",
            total_played_seconds=0.0,
        )

        assert "totalPlayedSeconds" in feedback["event"]
        assert feedback["event"]["totalPlayedSeconds"] == 0.0

    def test_build_feedback_structure_nested(self):
        """build_feedback имеет полезную нагрузку во вложенном event."""
        feedback = build_feedback(
            "skip",
            batch_id="batch1",
            track_id="track1",
            total_played_seconds=5.0,
        )

        assert isinstance(feedback["event"], dict)
        assert feedback["event"]["type"] == "skip"
        assert feedback["event"]["trackId"] == "track1"
        assert feedback["event"]["totalPlayedSeconds"] == 5.0
        # batchId и from - соседи на верхнем уровне, не в event
        assert "batchId" in feedback
        assert "trackId" not in feedback  # trackId в event, не в корне

    def test_fetch_session_tracks_with_empty_feedbacks_no_key(self):
        """Если feedbacks пуст, ключ feedbacks не добавляется в тело запроса."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value={"batchId": "b1", "sequence": []})

        from bot.yandex.client import YandexMusicClient
        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        import asyncio
        asyncio.run(client.fetch_session_tracks(queue=[], feedbacks=[]))

        call_args = mock_client.request.post.call_args
        payload = call_args[1]["json"]
        assert "feedbacks" not in payload
        assert payload["queue"] == []

    def test_fetch_session_tracks_with_feedbacks_included(self):
        """Если feedbacks не пуст, ключ feedbacks включается в тело запроса."""
        mock_client = MagicMock()
        mock_client.base_url = "https://api.music.yandex.net"
        mock_client.request = MagicMock()
        mock_client.request.post = AsyncMock(return_value={"batchId": "b1", "sequence": []})

        from bot.yandex.client import YandexMusicClient
        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client)

        feedbacks = [{"event": {"type": "skip"}}]
        import asyncio
        asyncio.run(client.fetch_session_tracks(queue=[], feedbacks=feedbacks))

        call_args = mock_client.request.post.call_args
        payload = call_args[1]["json"]
        assert "feedbacks" in payload
        assert payload["feedbacks"] == feedbacks


class TestStartSessionRadioStartedFeedback:
    """Тесты что radioStarted отправляется при start_session."""

    @pytest.mark.asyncio
    async def test_start_session_sends_radio_started_feedback(self):
        """start_session() отправляет radioStarted фидбек на одиночный .../feedback/.

        Мутационная защита: раньше тест был слишком слаб (только проверял наличие
        "feedback" в URL). Теперь усилен:
        - Проверяет что отправка идёт на .../feedback/ (единственное число, не .../feedbacks/)
        - Проверяет что тело это сам фидбек, без обёртки {"feedbacks": [...]}
        - Проверяет что внутри фидбека есть radioStarted
        """
        mock_session_new_response = {
            "radioSessionId": "sid123",
            "batchId": "b1",
            "sequence": [],
        }

        mock_client_obj = MagicMock()
        mock_client_obj.base_url = "https://api.music.yandex.net"
        mock_client_obj.request = MagicMock()

        captured_calls = {"feedback_calls": [], "feedbacks_calls": []}

        async def capture_post(url, **kwargs):
            if "/feedback/" in url and "/feedbacks/" not in url:
                # Одиночный эндпоинт .../feedback/
                captured_calls["feedback_calls"].append((url, kwargs))
            elif "/feedbacks/" in url:
                # Пакетный эндпоинт .../feedbacks/
                captured_calls["feedbacks_calls"].append((url, kwargs))
            return mock_session_new_response

        mock_client_obj.request.post = AsyncMock(side_effect=capture_post)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client_obj)

        await client.start_session()

        # Проверяем что отправка была ровно на одиночный .../feedback/
        assert len(captured_calls["feedback_calls"]) > 0, (
            "После start_session должен быть вызов на .../feedback/ для radioStarted"
        )
        # Убедимся что на .../feedbacks/ (пакетный) НЕ было вызова
        assert len(captured_calls["feedbacks_calls"]) == 0, (
            "radioStarted должен отправляться на .../feedback/, а не на .../feedbacks/"
        )

        # Проверяем что в теле фидбека есть radioStarted, без обёртки feedbacks
        url, kwargs = captured_calls["feedback_calls"][0]
        feedback_body = kwargs.get("json")
        assert feedback_body is not None
        # Проверяем что это не обёрнуто в {"feedbacks": [...]}
        assert "feedbacks" not in feedback_body, (
            'Одиночный фидбек не должен быть обёрнут в {"feedbacks": [...]}'
        )
        # Проверяем что это radioStarted
        assert "event" in feedback_body
        assert feedback_body["event"]["type"] == "radioStarted"
        # Проверяем что from дублируется
        assert "from" in feedback_body
        assert feedback_body["event"]["from"] == feedback_body["from"]


class TestSessionNewRequestBody:
    """Тесты что session/new содержит правильные поля."""

    @pytest.mark.asyncio
    async def test_session_new_includes_include_wave_model_and_interactive(self):
        """session/new запрос содержит includeWaveModel и interactive."""
        mock_client_obj = MagicMock()
        mock_client_obj.base_url = "https://api.music.yandex.net"
        mock_client_obj.request = MagicMock()

        post_calls = []

        async def capture_post(url, **kwargs):
            post_calls.append((url, kwargs))
            return {"radioSessionId": "sid123", "batchId": "b1", "sequence": []}

        mock_client_obj.request.post = AsyncMock(side_effect=capture_post)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client_obj)

        await client.start_session()

        # Находим вызов session/new
        session_new_calls = [
            (url, kwargs) for url, kwargs in post_calls
            if "session/new" in url
        ]
        assert len(session_new_calls) > 0

        call_url, call_kwargs = session_new_calls[0]
        json_body = call_kwargs.get("json", {})

        assert "includeWaveModel" in json_body, (
            f"session/new должен содержать includeWaveModel, получено {json_body.keys()}"
        )
        assert json_body["includeWaveModel"] is True

        assert "interactive" in json_body, (
            f"session/new должен содержать interactive, получено {json_body.keys()}"
        )
        assert json_body["interactive"] is True


class TestFeedbackIdWithoutAlbum:
    """Тесты что feedback_id собирается правильно для трека без альбома."""

    @pytest.mark.asyncio
    async def test_feedback_id_fallback_without_album(self):
        """Трек без альбомов использует str(track_id) как feedback_id."""
        # Трек без альбомов
        track_no_album = create_mock_track("777", "No Album Track", "Artist", 180000, True, None)

        raw = {
            "batchId": "batch1",
            "sequence": [{"track": track_no_album}],
        }

        mock_client_obj = MagicMock()
        mock_client_obj.base_url = "https://api.music.yandex.net"
        mock_client_obj.request = MagicMock()
        mock_client_obj.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client_obj)

        with patch("bot.yandex.client.Track.de_json", return_value=track_no_album):
            batch = await client.fetch_session_tracks(queue=[], feedbacks=[])

        # Проверяем что feedback_id построен правильно (без альбома)
        assert len(batch.tracks) == 1
        # Когда альбомов нет, feedback_id должен быть просто str(id)
        assert batch.tracks[0].feedback_id == "777", (
            f"Без альбома feedback_id должен быть '777', получено "
            f"{batch.tracks[0].feedback_id}"
        )

    @pytest.mark.asyncio
    async def test_feedback_id_with_album(self):
        """Трек с альбомом использует <track_id>:<album_id> как feedback_id.

        Это основной случай сборки составного идентификатора.
        При мутации album_id = None тест должен упасть.
        """
        # Трек с альбомом: id="999", album.id="888" (явно различаются)
        track_with_album = create_mock_track("999", "Album Track", "Artist", 180000, True, "888")

        raw = {
            "batchId": "batch1",
            "sequence": [{"track": track_with_album}],
        }

        mock_client_obj = MagicMock()
        mock_client_obj.base_url = "https://api.music.yandex.net"
        mock_client_obj.request = MagicMock()
        mock_client_obj.request.post = AsyncMock(return_value=raw)

        client = YandexMusicClient("token123")
        client._radio_session_id = "session123"
        set_mock_client(client, mock_client_obj)

        with patch("bot.yandex.client.Track.de_json", return_value=track_with_album):
            batch = await client.fetch_session_tracks(queue=[], feedbacks=[])

        # Проверяем что feedback_id собран правильно (с альбомом)
        assert len(batch.tracks) == 1
        # Должен быть составной идентификатор <track_id>:<album_id>
        assert batch.tracks[0].feedback_id == "999:888", (
            f"С альбомом feedback_id должен быть '999:888', получено "
            f"{batch.tracks[0].feedback_id}"
        )


class TestYandexMusicClientSearchTracks:
    """Тесты для search_tracks()."""

    @pytest.mark.asyncio
    async def test_search_tracks_returns_tuple_of_track_info(self):
        """search_tracks() возвращает кортеж TrackInfo."""
        track1 = create_mock_track("track1", "Song 1", "Artist A", 180000, True, "album1")
        track2 = create_mock_track("track2", "Song 2", "Artist B", 240000, True, "album2")

        search_result = SimpleNamespace(
            tracks=SimpleNamespace(results=[track1, track2])
        )
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=search_result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch("bot.yandex.client.Track.de_json", side_effect=[track1, track2]):
            result = await client.search_tracks("test query")

        assert isinstance(result, tuple)
        assert len(result) == 2
        assert all(isinstance(t, TrackInfo) for t in result)
        assert result[0].id == "track1"
        assert result[1].id == "track2"

    @pytest.mark.asyncio
    async def test_search_tracks_respects_limit(self):
        """search_tracks() возвращает не больше limit треков."""
        tracks = [
            create_mock_track(
                f"track{i}",
                f"Song {i}",
                "Artist",
                180000,
                True,
                f"album{i}",
            )
            for i in range(5)
        ]

        search_result = SimpleNamespace(tracks=SimpleNamespace(results=tracks))
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=search_result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch("bot.yandex.client.Track.de_json", side_effect=tracks):
            result = await client.search_tracks("test query", limit=3)

        assert len(result) == 3
        assert result[0].id == "track0"
        assert result[2].id == "track2"

    @pytest.mark.asyncio
    async def test_search_tracks_skips_unavailable_tracks(self):
        """search_tracks() пропускает недоступные треки (available=False)."""
        available_track = create_mock_track(
            "track1", "Available", "Artist", 180000, True, "album1"
        )
        unavailable_track = create_mock_track(
            "track2", "Unavailable", "Artist", 180000, False, "album2"
        )

        search_result = SimpleNamespace(
            tracks=SimpleNamespace(results=[available_track, unavailable_track])
        )
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=search_result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch(
            "bot.yandex.client.Track.de_json",
            side_effect=[available_track, unavailable_track],
        ):
            result = await client.search_tracks("test query")

        assert len(result) == 1
        assert result[0].id == "track1"

    @pytest.mark.asyncio
    async def test_search_tracks_empty_result_returns_empty_tuple(self):
        """search_tracks() с пустой выдачей возвращает пустой кортеж."""
        search_result = SimpleNamespace(tracks=None)
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=search_result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        result = await client.search_tracks("nonexistent")

        assert result == ()
        assert isinstance(result, tuple)

    @pytest.mark.asyncio
    async def test_search_tracks_empty_tracks_list_returns_empty_tuple(self):
        """search_tracks() с пустым списком результатов возвращает пустой кортеж."""
        search_result = SimpleNamespace(tracks=SimpleNamespace(results=[]))
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=search_result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        result = await client.search_tracks("nonexistent")

        assert result == ()

    @pytest.mark.asyncio
    async def test_search_tracks_constructs_composite_feedback_id(self):
        """search_tracks() собирает feedback_id как <track_id>:<album_id>."""
        track = create_mock_track("track123", "Song", "Artist", 180000, True, "album456")

        search_result = SimpleNamespace(tracks=SimpleNamespace(results=[track]))
        mock_client = MagicMock()
        mock_client.search = AsyncMock(return_value=search_result)

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with patch("bot.yandex.client.Track.de_json", return_value=track):
            result = await client.search_tracks("test")

        assert len(result) == 1
        assert result[0].feedback_id == "track123:album456"

    @pytest.mark.asyncio
    async def test_search_tracks_unauthorized_error_raises_auth_error(self):
        """search_tracks() с UnauthorizedError выбрасывает YandexAuthError."""
        mock_client = MagicMock()
        mock_client.search = AsyncMock(
            side_effect=yandex_music.exceptions.UnauthorizedError("token expired")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(YandexAuthError):
            await client.search_tracks("test")

    @pytest.mark.asyncio
    async def test_search_tracks_yandex_music_error_raises_search_unavailable(self):
        """search_tracks() с YandexMusicError выбрасывает SearchUnavailableError."""
        mock_client = MagicMock()
        mock_client.search = AsyncMock(
            side_effect=yandex_music.exceptions.YandexMusicError("server error")
        )

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(SearchUnavailableError):
            await client.search_tracks("test")

    @pytest.mark.asyncio
    async def test_search_tracks_os_error_raises_search_unavailable(self):
        """search_tracks() с OSError выбрасывает SearchUnavailableError."""
        mock_client = MagicMock()
        mock_client.search = AsyncMock(side_effect=OSError("connection error"))

        client = YandexMusicClient("token123")
        set_mock_client(client, mock_client)

        with pytest.raises(SearchUnavailableError):
            await client.search_tracks("test")


class TestYandexMusicClientCoverUrl:
    """Тесты для извлечения cover_url в _track_info_from_track."""

    def test_track_info_from_track_extracts_cover_uri_with_correct_size(self):
        """_track_info_from_track использует cover_uri и подставляет размер 400x400."""
        cover_template = (
            "avatars.yandex.net/get-music-content/95061/4f3808a0.a.5307396-3/%%"
        )
        track = create_mock_track(
            "track123",
            "Test Song",
            "Test Artist",
            duration_ms=180000,
            available=True,
            album_id="album456",
            cover_uri=cover_template,
        )

        track_info = YandexMusicClient._track_info_from_track(track)

        # Проверяем что cover_url содержит размер 400x400
        assert track_info is not None
        assert track_info.cover_url is not None
        assert "400x400" in track_info.cover_url
        assert track_info.cover_url == cover_template.replace("%%", "400x400")

    def test_track_info_from_track_fallback_og_image_when_no_cover_uri(self):
        """_track_info_from_track использует og_image если cover_uri отсутствует."""
        og_template = "avatars.yandex.net/get-music-content/backup/og_image/%%"
        track = create_mock_track(
            "track123",
            "Test Song",
            "Test Artist",
            duration_ms=180000,
            available=True,
            album_id="album456",
            cover_uri=None,
            og_image=og_template,
        )

        track_info = YandexMusicClient._track_info_from_track(track)

        # Проверяем что используется og_image с размером 400x400
        assert track_info is not None
        assert track_info.cover_url is not None
        assert "400x400" in track_info.cover_url
        assert track_info.cover_url == og_template.replace("%%", "400x400")

    def test_track_info_from_track_cover_url_none_when_no_cover_sources(self):
        """_track_info_from_track возвращает cover_url=None если нет cover_uri или og_image."""
        track = create_mock_track(
            "track123",
            "Test Song",
            "Test Artist",
            duration_ms=180000,
            available=True,
            album_id="album456",
            cover_uri=None,
            og_image=None,
        )

        track_info = YandexMusicClient._track_info_from_track(track)

        # Проверяем что cover_url None
        assert track_info is not None
        assert track_info.cover_url is None
