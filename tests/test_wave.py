"""Tests for bot.yandex.wave module."""


import pytest

from bot.errors import WaveUnavailableError
from bot.yandex.client import TrackInfo, WaveBatch
from bot.yandex.wave import WaveSession


class FakeMusicClient:
    """Fake YandexMusicClient for testing."""

    def __init__(self):
        self.calls = []
        self.batches_to_return = []
        self.batch_index = 0

    async def start_wave(self, *, from_=None, batch_id=None):
        self.calls.append(("start_wave", from_, batch_id))

    async def fetch_wave_batch(self, queue=None):
        self.calls.append(("fetch_wave_batch", queue))
        if self.batch_index < len(self.batches_to_return):
            batch = self.batches_to_return[self.batch_index]
            self.batch_index += 1
            return batch
        return WaveBatch(batch_id=None, tracks=())

    async def notify_track_started(self, track_id, batch_id):
        self.calls.append(("notify_track_started", track_id, batch_id))

    async def notify_track_finished(self, track_id, played_seconds, batch_id):
        self.calls.append(("notify_track_finished", track_id, played_seconds, batch_id))

    async def notify_track_skipped(self, track_id, played_seconds, batch_id):
        self.calls.append(("notify_track_skipped", track_id, played_seconds, batch_id))


def make_track(track_id, title="Song"):
    """Helper to create a TrackInfo."""
    return TrackInfo(id=track_id, title=title, artists="Artist", duration=180.0, raw=None)


class TestWaveSessionBasics:
    """Basic WaveSession tests."""

    def test_new_session_not_started(self):
        """New session hasn't started."""
        client = FakeMusicClient()
        session = WaveSession(client)
        assert session.started is False

    def test_new_session_empty_buffer(self):
        """New session has empty buffer."""
        client = FakeMusicClient()
        session = WaveSession(client)
        assert session.buffered == 0

    def test_max_fetch_attempts_constant(self):
        """MAX_FETCH_ATTEMPTS == 3."""
        assert WaveSession.MAX_FETCH_ATTEMPTS == 3


class TestWaveSessionStart:
    """Tests for start() method."""

    @pytest.mark.asyncio
    async def test_start_marks_started(self):
        """start() sets started=True."""
        client = FakeMusicClient()
        session = WaveSession(client)

        await session.start()

        assert session.started is True

    @pytest.mark.asyncio
    async def test_start_calls_client(self):
        """start() calls client.start_wave()."""
        client = FakeMusicClient()
        session = WaveSession(client)

        await session.start()

        assert any(call[0] == "start_wave" for call in client.calls)


class TestWaveSessionNextTrack:
    """Tests for next_track() method."""

    @pytest.mark.asyncio
    async def test_next_track_without_start_raises_error(self):
        """next_track() without start() raises WaveUnavailableError."""
        client = FakeMusicClient()
        session = WaveSession(client)

        with pytest.raises(WaveUnavailableError):
            await session.next_track()

    @pytest.mark.asyncio
    async def test_next_track_returns_tracks_in_order(self):
        """next_track() returns tracks in order from batch."""
        track1 = make_track("1", "Song1")
        track2 = make_track("2", "Song2")
        batch = WaveBatch(batch_id="batch1", tracks=(track1, track2))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        t1 = await session.next_track()
        assert t1.id == "1"
        assert session.buffered == 1

        t2 = await session.next_track()
        assert t2.id == "2"
        assert session.buffered == 0

    @pytest.mark.asyncio
    async def test_next_track_fetches_new_batch_when_empty(self):
        """next_track() fetches new batch when buffer empty."""
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        await session.next_track()
        await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) == 2

    @pytest.mark.asyncio
    async def test_next_track_passes_last_track_id_as_queue(self):
        """next_track() passes last_track_id as queue parameter."""
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        await session.next_track()
        await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert fetch_calls[1][1] == "1"  # queue should be previous track_id

    @pytest.mark.asyncio
    async def test_all_empty_batches_raise_error(self):
        """All empty batches → WaveUnavailableError."""
        client = FakeMusicClient()
        client.batches_to_return = [
            WaveBatch(batch_id="b1", tracks=()),
            WaveBatch(batch_id="b2", tracks=()),
            WaveBatch(batch_id="b3", tracks=()),
        ]
        session = WaveSession(client)

        await session.start()

        with pytest.raises(WaveUnavailableError):
            await session.next_track()

        # Should have tried MAX_FETCH_ATTEMPTS times
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) == WaveSession.MAX_FETCH_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retry_on_empty_then_success(self):
        """First batch empty, second has track → success."""
        batch1 = WaveBatch(batch_id="b1", tracks=())
        batch2 = WaveBatch(batch_id="b2", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()

        assert track.id == "1"
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) == 2


class TestWaveSessionUpcoming:
    """Tests for upcoming() method."""

    @pytest.mark.asyncio
    async def test_upcoming_returns_preview(self):
        """upcoming() returns buffered tracks without modifying buffer."""
        batch = WaveBatch(
            batch_id="b1",
            tracks=(make_track("1"), make_track("2"), make_track("3")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        # Consume first track to fill buffer with the batch
        await session.next_track()

        preview = session.upcoming(2)

        assert len(preview) == 2
        assert preview[0].id == "2"
        assert preview[1].id == "3"
        assert session.buffered == 2  # Buffer unchanged

    @pytest.mark.asyncio
    async def test_upcoming_zero_limit(self):
        """upcoming(0) returns empty list."""
        batch = WaveBatch(batch_id="b1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        preview = session.upcoming(0)

        assert preview == []

    @pytest.mark.asyncio
    async def test_upcoming_negative_limit(self):
        """upcoming(-1) returns empty list."""
        batch = WaveBatch(batch_id="b1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        preview = session.upcoming(-1)

        assert preview == []

    @pytest.mark.asyncio
    async def test_upcoming_limit_exceeds_buffer(self):
        """upcoming(100) with 3 tracks returns 3."""
        batch = WaveBatch(
            batch_id="b1",
            tracks=(make_track("1"), make_track("2"), make_track("3")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        # Consume first track to fill buffer
        await session.next_track()

        preview = session.upcoming(100)

        assert len(preview) == 2  # 3 total tracks - 1 consumed


class TestWaveSessionFeedback:
    """Tests for track_started/finished/skipped methods."""

    @pytest.mark.asyncio
    async def test_track_started_calls_client(self):
        """track_started() calls client.notify_track_started()."""
        batch = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()
        await session.track_started(track)

        assert ("notify_track_started", "1", "batch1") in client.calls

    @pytest.mark.asyncio
    async def test_track_finished_normalizes_played_seconds(self):
        """track_finished() normalizes negative played_seconds to 0."""
        batch = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()
        await session.track_finished(track, -5.0)

        finished_calls = [c for c in client.calls if c[0] == "notify_track_finished"]
        assert len(finished_calls) == 1
        assert finished_calls[0][2] == 0.0  # played_seconds should be 0.0

    @pytest.mark.asyncio
    async def test_track_skipped_normalizes_played_seconds(self):
        """track_skipped() normalizes played_seconds."""
        batch = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()
        await session.track_skipped(track, -5.0)

        skipped_calls = [c for c in client.calls if c[0] == "notify_track_skipped"]
        assert len(skipped_calls) == 1
        assert skipped_calls[0][2] == 0.0


class TestWaveSessionBatchId:
    """Tests for batch_id property."""

    @pytest.mark.asyncio
    async def test_batch_id_from_fetch(self):
        """batch_id comes from fetched batch."""
        batch = WaveBatch(batch_id="custom-batch-123", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        assert session.batch_id is None

        await session.next_track()
        assert session.batch_id == "custom-batch-123"

    @pytest.mark.asyncio
    async def test_batch_id_preserved_across_empty_batch_retry(self, monkeypatch):
        """batch_id is not overwritten by empty batch during retry.

        Regression test: in next_track(), when an empty batch is fetched,
        _batch_id should NOT be updated to None. It should only update
        when a non-empty batch arrives.

        Scenario:
        1. First batch has batch_id="batch-1" with 2 tracks
        2. Second fetch returns empty batch (simulates retry/no data)
        3. Third fetch returns batch_id="batch-2" with 1 track
        4. After consuming batch-1 and retrying, batch_id should stay non-None
        5. When track_started is called, it should use the valid batch_id
        """
        # Mock asyncio.sleep to avoid delays
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        # Create batches: first non-empty, then empty, then non-empty again
        batch1 = WaveBatch(batch_id="batch-1", tracks=(make_track("1"), make_track("2")))
        batch_empty = WaveBatch(batch_id=None, tracks=())
        batch2 = WaveBatch(batch_id="batch-2", tracks=(make_track("3"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch_empty, batch2]
        session = WaveSession(client)

        await session.start()

        # Consume both tracks from first batch
        track1 = await session.next_track()
        assert track1.id == "1"
        assert session.batch_id == "batch-1"

        track2 = await session.next_track()
        assert track2.id == "2"
        # Буфер пуст, но batch_id сохранён
        assert session.batch_id == "batch-1"

        # Request another track — will trigger fetch, hit empty batch, retry, get batch-2
        track3 = await session.next_track()
        assert track3.id == "3"
        assert session.batch_id == "batch-2"

        # Now send track_started for track3 while batch_id is batch-2
        await session.track_started(track3)

        # Verify the feedback was sent with the correct (non-None) batch_id
        started_calls = [c for c in client.calls if c[0] == "notify_track_started"]
        assert len(started_calls) == 1
        assert started_calls[0][1] == "3"
        assert started_calls[0][2] == "batch-2"  # batch_id must NOT be None
