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


class TestWaveSessionSkipAndAdaptation:
    """Tests for wave adaptation upon track skip and deduplication."""

    @pytest.mark.asyncio
    async def test_buffer_cleared_on_skip(self):
        """track_skipped() clears the buffer completely."""
        # Set up: multiple tracks in batch
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")
        track_c = make_track("c", "Track C")
        batch = WaveBatch(batch_id="batch1", tracks=(track_a, track_b, track_c))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        # Take first track, verify buffer has 2 remaining
        taken = await session.next_track()
        assert taken.id == "a"
        assert session.buffered == 2

        # Skip the taken track
        await session.track_skipped(taken, 10.0)

        # Buffer should now be empty
        assert session.buffered == 0

    @pytest.mark.asyncio
    async def test_skip_feedback_before_buffer_clear(self, monkeypatch):
        """Feedback notify_track_skipped is sent BEFORE subsequent fetch.

        When track_skipped() is called, it sends feedback first, then clears
        the buffer. The next next_track() will fetch a fresh batch. We verify
        that the skip feedback appears in the call log BEFORE the fetch that
        happens in the subsequent next_track() call.

        This is essential: if buffer were cleared first without sending feedback,
        the server would regenerate the wave without accounting for the skip.
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")
        track_c = make_track("c", "Track C")

        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
        batch2 = WaveBatch(batch_id="batch2", tracks=(track_c,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        taken = await session.next_track()

        # At this point: calls = [start_wave, fetch_wave_batch]
        calls_before_skip = len(client.calls)

        await session.track_skipped(taken, 5.0)

        # After skip: calls should include notify_track_skipped
        skip_calls = [i for i, c in enumerate(client.calls) if c[0] == "notify_track_skipped"]
        assert len(skip_calls) == 1

        # Now call next_track again, which will fetch because buffer is empty
        await session.next_track()

        # Find all fetch calls; the second one should come AFTER notify_track_skipped
        all_calls = client.calls
        skip_index = None
        first_new_fetch_index = None

        for i, call in enumerate(all_calls):
            if call[0] == "notify_track_skipped":
                skip_index = i
            elif call[0] == "fetch_wave_batch" and i > calls_before_skip:
                # This is the fetch triggered by the second next_track()
                first_new_fetch_index = i
                break

        assert skip_index is not None
        assert first_new_fetch_index is not None
        assert skip_index < first_new_fetch_index, (
            f"notify_track_skipped (index {skip_index}) must come before "
            f"subsequent fetch_wave_batch (index {first_new_fetch_index})"
        )

    @pytest.mark.asyncio
    async def test_next_track_after_skip_uses_skipped_id_as_queue(self, monkeypatch):
        """After skip, fetch_wave_batch is called with skipped track's id as queue param."""
        # Mock sleep to avoid delays
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")
        track_c = make_track("c", "Track C")
        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
        batch2 = WaveBatch(batch_id="batch2", tracks=(track_c,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()

        # Get first track and skip it
        taken = await session.next_track()
        assert taken.id == "a"
        await session.track_skipped(taken, 5.0)

        # Now fetch next track; buffer is empty so it should fetch
        await session.next_track()

        # Find the fetch_wave_batch call that happened after the skip
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        # We expect: initial start_wave triggers first fetch implicitly (or not),
        # then next_track after skip should pass queue="a"
        # Let's check that the most recent fetch has queue="a"
        assert fetch_calls[-1][1] == "a", (
            f"Most recent fetch should have queue='a' (the skipped track id), "
            f"but got queue={fetch_calls[-1][1]!r}"
        )

    @pytest.mark.asyncio
    async def test_duplicates_filtered_from_new_batch(self):
        """Recently played tracks are filtered out from new batches."""
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")
        track_c = make_track("c", "Track C")

        # First batch: A and B
        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
        # Second batch: A again (duplicate) and new track C
        batch2 = WaveBatch(batch_id="batch2", tracks=(track_a, track_c))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()

        # Consume both tracks from first batch
        t1 = await session.next_track()
        assert t1.id == "a"

        t2 = await session.next_track()
        assert t2.id == "b"

        # Buffer is empty, so next call fetches second batch
        # Second batch contains A (duplicate) and C (new)
        # A should be filtered out, C should be returned
        t3 = await session.next_track()
        assert t3.id == "c", (
            f"Expected track C (new track), but got {t3.id}. "
            f"Track A should have been filtered as a duplicate."
        )

    @pytest.mark.asyncio
    async def test_all_duplicates_fallback_to_unfiltered(self, monkeypatch):
        """If batch contains only recent tracks and it's the last attempt, use unfiltered.

        Protection against infinite loop: if all tracks in batch are recent
        and MAX_FETCH_ATTEMPTS is exhausted, return a track anyway (even duplicate)
        rather than raise an error. Music should not stop because of dedup logic.
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")

        # First batch: A and B
        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
        # All subsequent batches: only A (duplicate)
        batch_dup = WaveBatch(batch_id="batch-dup", tracks=(track_a,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch_dup, batch_dup, batch_dup]
        session = WaveSession(client)

        await session.start()

        # Consume first batch
        t1 = await session.next_track()
        assert t1.id == "a"
        t2 = await session.next_track()
        assert t2.id == "b"

        # Now: buffer empty, next fetch returns only A (which is recent)
        # On attempt 1 and 2: filtered out, retry
        # On attempt 3 (MAX_FETCH_ATTEMPTS): filtered but no more retries, use unfiltered
        # Result: should return A without raising WaveUnavailableError
        t3 = await session.next_track()
        assert t3.id == "a", (
            "Should return duplicate track A as fallback when all tracks in "
            "final batch are duplicates (better to repeat than to stop music)"
        )

    @pytest.mark.asyncio
    async def test_start_clears_history(self):
        """start() clears recent track history.

        After consuming track A and restarting, a new batch containing A
        should return A again (no dedup across restart boundaries).
        """
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")

        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
        batch2 = WaveBatch(batch_id="batch2", tracks=(track_a,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        # First session
        await session.start()
        t1 = await session.next_track()
        assert t1.id == "a"

        # Restart: should clear recent history
        await session.start()

        # Fetch batch2 containing A; despite A being in recent from before restart,
        # history was cleared, so A should be returned (not filtered)
        t2 = await session.next_track()
        assert t2.id == "a", (
            "After restart, history should be cleared; "
            "previously played track A should be playable again"
        )

    @pytest.mark.asyncio
    async def test_empty_batches_still_raise_error(self):
        """Completely empty batches (no tracks at all) still raise WaveUnavailableError.

        This is distinct from batches containing only duplicates.
        Empty batch is a network/service failure, not a dedup issue.
        """
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
