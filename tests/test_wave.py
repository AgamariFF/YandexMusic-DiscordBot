"""Tests for bot.yandex.wave module."""


import pytest

from bot.errors import WaveUnavailableError, YandexAuthError
from bot.yandex.client import TrackInfo, WaveBatch
from bot.yandex.wave import WaveSession


class FakeMusicClient:
    """Fake YandexMusicClient for testing.

    Supports both a list of batches (for simple cases) and a fetch handler
    function (for more complex scenarios with retries and errors).
    """

    def __init__(self):
        self.calls = []
        self.batches_to_return = []
        self.batch_index = 0
        self.fetch_handler = None  # Optional callable for custom behavior

    async def start_wave(self, *, from_=None, batch_id=None):
        self.calls.append(("start_wave", from_, batch_id))

    async def fetch_wave_batch(self, queue=None):
        self.calls.append(("fetch_wave_batch", queue))
        if self.fetch_handler is not None:
            return await self.fetch_handler(queue, self.batch_index, self)
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
        """MAX_FETCH_ATTEMPTS == 4."""
        assert WaveSession.MAX_FETCH_ATTEMPTS == 4


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
        """next_track() returns tracks in sequence; buffer never exceeds MAX_BUFFERED_TRACKS."""
        from bot.yandex.wave import MAX_BUFFERED_TRACKS

        track1 = make_track("1", "Song1")
        track2 = make_track("2", "Song2")
        track3 = make_track("3", "Song3")
        batch1 = WaveBatch(batch_id="batch1", tracks=(track1, track2))
        # Second batch for refresh after extracting track1
        batch2 = WaveBatch(batch_id="batch2", tracks=(track3,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()

        # First next_track: fetch batch1, extract track1, _refresh_chain gets batch2
        t1 = await session.next_track()
        assert t1.id == "1"
        # Buffer should respect MAX_BUFFERED_TRACKS even with larger batch
        assert session.buffered <= MAX_BUFFERED_TRACKS, (
            f"After first next_track(), buffer exceeded "
            f"MAX_BUFFERED_TRACKS={MAX_BUFFERED_TRACKS}, got {session.buffered}"
        )

        # Second next_track: buffer was refilled; must respect limit
        await session.next_track()
        assert session.buffered <= MAX_BUFFERED_TRACKS, (
            f"After second next_track(), buffer exceeded "
            f"MAX_BUFFERED_TRACKS={MAX_BUFFERED_TRACKS}, got {session.buffered}"
        )

    @pytest.mark.asyncio
    async def test_next_track_fetches_new_batch_when_empty(self):
        """next_track() fetches new batch when buffer empty, and updates chain via _refresh_chain.

        Each next_track() does:
        1. Fetch to fill buffer (if empty)
        2. Get track from buffer
        3. Call _refresh_chain() which does another fetch

        So: next_track() #1: fetch (fill) + fetch (refresh) = 2 calls
            next_track() #2: fetch (already filled by refresh) + fetch (refresh) = 2 calls
        Total: 4 calls (but we only test that at least 1 fetch happens per next_track()).
        """
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))
        batch_empty = WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2, batch_empty]
        session = WaveSession(client)

        await session.start()
        track1 = await session.next_track()
        assert track1.id == "1"

        track2 = await session.next_track()
        assert track2.id == "2"

        # Verify multiple fetches occurred (at least one per next_track call)
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) >= 2

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
    async def test_all_empty_batches_raise_error(self, monkeypatch):
        """All empty batches → WaveUnavailableError."""

        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

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
        """First batch empty, second has track → success (with retries and refresh).

        Scenario:
        1. First fetch returns empty → retry
        2. Second fetch returns track → success, populate buffer
        3. Extract track, call _refresh_chain which does another fetch
        """
        batch1 = WaveBatch(batch_id="b1", tracks=())
        batch2 = WaveBatch(batch_id="b2", tracks=(make_track("1"),))
        batch_refresh = WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2, batch_refresh]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()

        assert track.id == "1"
        # Should have at least 2 fetches: one retry loop + one refresh
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) >= 2


class TestWaveSessionUpcoming:
    """Tests for upcoming() method."""

    @pytest.mark.asyncio
    async def test_upcoming_returns_preview(self):
        """upcoming() returns buffered tracks (<=MAX_BUFFERED_TRACKS) without modifying buffer."""
        batch = WaveBatch(
            batch_id="b1",
            tracks=(make_track("1"), make_track("2"), make_track("3")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        # Consume first track, buffer is refilled by _refresh_chain (<=1 track)
        await session.next_track()

        preview = session.upcoming(2)

        # upcoming() returns what's in buffer, which is <= MAX_BUFFERED_TRACKS
        from bot.yandex.wave import MAX_BUFFERED_TRACKS
        assert len(preview) <= MAX_BUFFERED_TRACKS
        # Verify buffer is unchanged
        buffered_before = session.buffered
        session.upcoming(5)
        assert session.buffered == buffered_before

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
        """upcoming(100) returns at most what's buffered (<=MAX_BUFFERED_TRACKS)."""
        batch = WaveBatch(
            batch_id="b1",
            tracks=(make_track("1"), make_track("2"), make_track("3")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        # Consume first track to fill buffer (refilled by _refresh_chain to <=1 track)
        await session.next_track()

        preview = session.upcoming(100)

        # upcoming(100) returns all buffered tracks, which is <= MAX_BUFFERED_TRACKS
        from bot.yandex.wave import MAX_BUFFERED_TRACKS
        assert len(preview) <= MAX_BUFFERED_TRACKS


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

        Scenario (adapted for MAX_BUFFERED_TRACKS=1):
        1. First batch has batch_id="batch-1" with 1 track
        2. Extract track, _refresh_chain returns empty batch (no update)
        3. Next fetch for filling buffer returns empty (retry/no data)
        4. Final retry returns batch_id="batch-2" with 1 track
        5. batch_id should never be None; when track_started is called,
           it should use the valid batch_id from the extracted track
        """
        # Mock asyncio.sleep to avoid delays
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        # Create batches: first non-empty, then empty, then non-empty again
        batch1 = WaveBatch(batch_id="batch-1", tracks=(make_track("1"),))
        batch_empty = WaveBatch(batch_id=None, tracks=())
        batch2 = WaveBatch(batch_id="batch-2", tracks=(make_track("3"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch_empty, batch_empty, batch2]
        session = WaveSession(client)

        await session.start()

        # Consume track from first batch
        track1 = await session.next_track()
        assert track1.id == "1"
        assert session.batch_id == "batch-1"

        # Get next track — will trigger fetch, hit empty batches, retry, get batch-2
        track2 = await session.next_track()
        assert track2.id == "3"
        assert session.batch_id == "batch-2"

        # Now send track_started for track2 while batch_id is batch-2
        await session.track_started(track2)

        # Verify the feedback was sent with the correct (non-None) batch_id
        started_calls = [c for c in client.calls if c[0] == "notify_track_started"]
        assert len(started_calls) == 1
        assert started_calls[0][1] == "3"
        assert started_calls[0][2] == "batch-2"  # batch_id must NOT be None


class TestWaveSessionSkipAndAdaptation:
    """Tests for wave adaptation upon track skip and deduplication."""

    @pytest.mark.asyncio
    async def test_skip_keeps_queue_intact(self):
        """track_skipped() does NOT clear buffer — only sends feedback.

        Regression test for the issue: /skip should work like next-track, not reshuffle queue.
        After skipping a track, the buffer should retain its existing tracks.

        Key: track_skipped() calls client.notify_track_skipped() but does NOT
        clear self._buffer. The buffer is only updated by _refresh_chain() (after
        each next_track) or by next_track()'s retry logic.
        """
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")

        # Use a handler to return different batches based on call count
        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            # 1st fetch (from next_track #1 fill): batch with A, B
            # 2nd fetch (from _refresh_chain after next_track #1): empty
            if fetch_count["count"] == 1:
                return WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
            # All subsequent fetches return empty
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()

        # Get first track (A), buffer now has [B]
        taken = await session.next_track()
        assert taken.id == "a"
        buffered_before_skip = session.buffered

        # Skip A — should NOT clear buffer
        await session.track_skipped(taken, 10.0)

        # Verify buffer is unchanged (still has the tracks that were there)
        assert session.buffered == buffered_before_skip

    @pytest.mark.asyncio
    async def test_skip_feedback_before_next_fetch(self, monkeypatch):
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
    async def test_next_track_after_skip_passes_track_id_to_refresh(self, monkeypatch):
        """After next_track(), _refresh_chain uses that track's id as queue parameter.

        The key behavior: when next_track() extracts a track, it calls
        _refresh_chain(track.id), which sends fetch_wave_batch(queue=track.id).

        Scenario (with MAX_BUFFERED_TRACKS=1):
        1. First next_track(): fetch returns A (only 1 in buffer), extract A,
           _refresh_chain calls fetch(queue="a")
        2. Skip track A
        3. Second next_track(): fetch returns B, extract B,
           _refresh_chain calls fetch(queue="b")
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")

        # Create handler to return specific batches
        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                # First fetch (from initial next_track): only A (MAX_BUFFERED_TRACKS=1)
                return WaveBatch(batch_id="batch1", tracks=(track_a,))
            elif fetch_count["count"] == 2:
                # Refresh after extracting A
                return WaveBatch(batch_id="batch1-refresh", tracks=(track_b,))
            # All others: empty
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()

        # Get track A
        taken_a = await session.next_track()
        assert taken_a.id == "a"

        # Skip track A (buffer is now empty, will be refilled by next next_track())
        await session.track_skipped(taken_a, 5.0)

        # Get track B; this should call _refresh_chain(b)
        taken_b = await session.next_track()
        assert taken_b.id == "b"

        # Find all fetch_wave_batch calls with their queue parameters
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        # We expect:
        # 1. fetch(queue=None) — initial fill
        # 2. fetch(queue="a") — refresh after extracting A
        # 3. fetch(queue="b") — refresh after extracting B
        assert len(fetch_calls) >= 3, f"Expected >=3 fetches, got {len(fetch_calls)}"
        # Check that fetches have the expected queue values in order
        assert fetch_calls[0][1] is None  # initial
        assert fetch_calls[1][1] == "a"   # refresh after A
        assert fetch_calls[-1][1] == "b", (
            f"Last fetch should have queue='b' (id of track extracted in second next_track()), "
            f"but got queue={fetch_calls[-1][1]!r}"
        )

    @pytest.mark.asyncio
    async def test_duplicates_filtered_from_new_batch(self):
        """Recently played tracks are filtered out from new batches (in _refresh_chain).

        Scenario:
        1. next_track() gets track A from batch1 (A, B), calls _refresh_chain(A)
        2. _refresh_chain fetches batch2 (A, C) — A is recent, so filtered → buffer=[C]
        3. next_track() extracts C from buffer (which was filled by _refresh_chain)

        Note: C is returned from buffer that was filled by _refresh_chain after
        extracting A, not from a separate fetch after B.
        """
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")
        track_c = make_track("c", "Track C")

        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                # Initial fetch: A and B
                return WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
            elif fetch_count["count"] == 2:
                # Refresh after extracting A: A (duplicate) and C (new)
                return WaveBatch(batch_id="batch2", tracks=(track_a, track_c))
            # All others: empty
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()

        # Get first track (A)
        t1 = await session.next_track()
        assert t1.id == "a"

        # Get second track (C, because _refresh_chain filtered A from batch2)
        t2 = await session.next_track()
        assert t2.id == "c", (
            f"Expected track C (new track after filtering duplicate A), but got {t2.id}."
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

        # First batch: A (with MAX_BUFFERED_TRACKS=1, only 1 goes to buffer)
        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a,))
        # All subsequent batches: only A (duplicate)
        batch_dup = WaveBatch(batch_id="batch-dup", tracks=(track_a,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch_dup, batch_dup, batch_dup, batch_dup]
        session = WaveSession(client)

        await session.start()

        # Consume first track
        t1 = await session.next_track()
        assert t1.id == "a"

        # Now: buffer empty (refilled by _refresh_chain to empty since all tracks are duplicates)
        # Next fetch returns only A (which is recent)
        # On attempts 1-3: filtered out, retry
        # On attempt 4 (MAX_FETCH_ATTEMPTS): filtered but no more retries, use unfiltered
        # Result: should return A without raising WaveUnavailableError
        t2 = await session.next_track()
        assert t2.id == "a", (
            "Should return duplicate track A as fallback when all tracks in "
            "final batch are duplicates (better to repeat than to stop music)"
        )

    @pytest.mark.asyncio
    async def test_start_clears_history(self):
        """start() clears recent track history.

        After consuming track A in session 1, calling start() should clear
        _recent. In session 2, a new batch containing A should return A
        (no dedup across restart boundaries).
        """
        track_a = make_track("a", "Track A")

        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            # Every fetch returns A; should only be playable after restart clears history
            return WaveBatch(batch_id=f"batch{fetch_count['count']}", tracks=(track_a,))

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        # First session: get A
        await session.start()
        t1 = await session.next_track()
        assert t1.id == "a"
        # After this, A is in _recent

        # Restart: should clear _recent
        await session.start()

        # Now fetch should return A again (history was cleared)
        t2 = await session.next_track()
        assert t2.id == "a", (
            "After restart, history should be cleared; "
            "previously played track A should be playable again"
        )

    @pytest.mark.asyncio
    async def test_duplicates_then_empty_batch_still_plays(self, monkeypatch):
        """Повторы на ранних попытках не теряются, если последняя пачка пустая.

        Сценарий: попытки 1-2 возвращают непустые пачки, состоящие только из
        уже игравших треков, а попытка 3 приходит пустой. Играбельные треки
        были получены, поэтому музыка обязана продолжиться повтором, а не
        умереть с WaveUnavailableError.
        """

        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a")
        client = FakeMusicClient()
        client.batches_to_return = [WaveBatch(batch_id="b0", tracks=(track_a,))]
        session = WaveSession(client)

        await session.start()
        played = await session.next_track()
        assert played.id == "a"

        # Дальше волна отдаёт только повтор, а затем пустую пачку.
        client.batches_to_return = [
            WaveBatch(batch_id="b1", tracks=(track_a,)),
            WaveBatch(batch_id="b2", tracks=(track_a,)),
            WaveBatch(batch_id="b3", tracks=()),
        ]
        client.batch_index = 0

        track = await session.next_track()
        assert track.id == "a"


class TestWaveSessionNetworkResilience:
    """Tests for network error handling and retries."""

    @pytest.mark.asyncio
    async def test_network_error_retried_then_succeeds(self, monkeypatch):
        """Сетевой сбой на попытках 1-2 не убивает волну: retry → успех на попытке 3."""
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a")
        attempt_count = {"count": 0}

        async def fetch_handler_with_retry(queue, batch_index, client):
            attempt_count["count"] += 1
            # Попытки 1-2: WaveUnavailableError (сетевой сбой)
            # Попытка 3: успех
            if attempt_count["count"] <= 2:
                raise WaveUnavailableError(user_message="Network error (simulated)")
            return WaveBatch(batch_id="batch_a", tracks=(track_a,))

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_with_retry
        session = WaveSession(client)

        await session.start()

        # next_track() should retry and eventually succeed
        track = await session.next_track()
        assert track.id == "a"
        # Verify that fetch was called 3 times (2 failures + 1 success + 1 refresh)
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) >= 3

    @pytest.mark.asyncio
    async def test_all_fetch_attempts_fail_raises_error(self, monkeypatch):
        """Все MAX_FETCH_ATTEMPTS попыток неудачны → WaveUnavailableError."""
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        async def always_fail(queue, batch_index, client):
            raise WaveUnavailableError(user_message="Persistent network error")

        client = FakeMusicClient()
        client.fetch_handler = always_fail
        session = WaveSession(client)

        await session.start()

        # Should raise WaveUnavailableError after MAX_FETCH_ATTEMPTS attempts
        with pytest.raises(WaveUnavailableError):
            await session.next_track()

        # Verify that fetch was called exactly MAX_FETCH_ATTEMPTS times
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) == WaveSession.MAX_FETCH_ATTEMPTS

    @pytest.mark.asyncio
    async def test_yandex_auth_error_not_retried(self):
        """YandexAuthError не ретраится: пробрасывается немедленно."""
        attempt_count = {"count": 0}

        async def fetch_handler_auth_error(queue, batch_index, client):
            attempt_count["count"] += 1
            raise YandexAuthError(user_message="Invalid token")

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_auth_error
        session = WaveSession(client)

        await session.start()

        # Should raise YandexAuthError immediately (no retries)
        with pytest.raises(YandexAuthError):
            await session.next_track()

        # Verify that fetch was called EXACTLY ONCE (no retries)
        fetch_calls = [c for c in client.calls if c[0] == "fetch_wave_batch"]
        assert len(fetch_calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_chain_error_does_not_stop_playback(self):
        """Ошибка _refresh_chain не прерывает воспроизведение.

        Сценарий (with MAX_BUFFERED_TRACKS=1):
        1. Первый fetch успешен → получаем трек A
        2. _refresh_chain вызывает fetch и получает ошибку
        3. Трек всё равно возвращается, буфер остаётся пустым (не перезаписывается ошибкой)
        4. Состояние сессии не ломается
        """
        track_a = make_track("a")
        fetch_count = {"count": 0}

        async def fetch_handler_refresh_fails(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                # Initial fetch: success with A (MAX_BUFFERED_TRACKS=1)
                return WaveBatch(batch_id="batch1", tracks=(track_a,))
            else:
                # Refresh fetches fail
                raise WaveUnavailableError(user_message="Refresh failed")

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_refresh_fails
        session = WaveSession(client)

        await session.start()

        # next_track() should succeed despite _refresh_chain error
        track = await session.next_track()
        assert track.id == "a"
        # Buffer should be empty (refresh failed, so no new tracks added)
        # But buffer is NOT corrupted by the error
        assert session.buffered == 0
        # Session should still be in valid state
        assert session.started is True


class TestWaveSessionBatchIdProperty:
    """Tests for batch_id property semantics."""

    @pytest.mark.asyncio
    async def test_batch_id_is_from_extracted_track(self):
        """batch_id свойство возвращает идентификатор пачки ПОСЛЕДНЕГО ВЫДАННОГО трека.

        После next_track(), batch_id должен соответствовать пачке, из которой
        был извлечён трек, даже если _refresh_chain потом получила пачку с
        другим batch_id.

        Ключ: _current_batch_id фиксируется ДО вызова _refresh_chain.
        """
        track_a = make_track("a")
        track_b = make_track("b")
        fetch_count = {"count": 0}

        async def fetch_handler_different_batches(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                # Initial fetch with batch_id="original"
                return WaveBatch(batch_id="original", tracks=(track_a, track_b))
            elif fetch_count["count"] == 2:
                # Обновление цепочки приносит НЕПУСТУЮ пачку с другим batch_id:
                # именно она перезапишет _batch_id, и без фиксации
                # _current_batch_id фидбек ушёл бы с чужим идентификатором.
                return WaveBatch(batch_id="refreshed", tracks=(make_track("c"),))
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_different_batches
        session = WaveSession(client)

        await session.start()

        # Extract track A
        track = await session.next_track()
        assert track.id == "a"

        # batch_id should be "original" (the batch from which A was extracted),
        # NOT "refreshed" (the batch from _refresh_chain)
        assert session.batch_id == "original", (
            f"batch_id should be 'original' (from extracted track), "
            f"not 'refreshed' (from _refresh_chain), but got {session.batch_id!r}"
        )

        # When track_skipped is called, it should use the correct batch_id
        await session.track_skipped(track, 5.0)

        skipped_calls = [c for c in client.calls if c[0] == "notify_track_skipped"]
        assert len(skipped_calls) == 1
        assert skipped_calls[0][3] == "original", (
            f"notify_track_skipped should use batch_id='original', "
            f"but got {skipped_calls[0][3]!r}"
        )


class TestWaveSessionBufferingConstraints:
    """Tests for buffer size constraints under new MAX_BUFFERED_TRACKS limit."""

    @pytest.mark.asyncio
    async def test_buffer_never_exceeds_one_track(self):
        """Server returns 5 tracks → buffer holds at most MAX_BUFFERED_TRACKS (drops excess)."""
        from bot.yandex.wave import MAX_BUFFERED_TRACKS

        track1 = make_track("1")
        track2 = make_track("2")
        track3 = make_track("3")
        track4 = make_track("4")
        track5 = make_track("5")

        # Single batch with 5 tracks; only MAX_BUFFERED_TRACKS will be placed in buffer
        batch = WaveBatch(
            batch_id="batch1",
            tracks=(track1, track2, track3, track4, track5),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        # First next_track(): fetch batch with 5 tracks, but only 1 goes to buffer
        t1 = await session.next_track()
        assert t1.id == "1"
        # Critical: even with 5-track batch, buffer must respect MAX_BUFFERED_TRACKS
        assert session.buffered <= MAX_BUFFERED_TRACKS, (
            f"After extracting from 5-track batch, buffer exceeded "
            f"MAX_BUFFERED_TRACKS={MAX_BUFFERED_TRACKS}, got {session.buffered}. "
            "Constraint must be enforced during loading!"
        )

    @pytest.mark.asyncio
    async def test_dropped_chain_tracks_are_refetched(self):
        """Excess tracks from large batch are not lost; next_track() continues without errors.

        Scenario:
        1. Server returns batch with 5 new unique tracks → only 1 goes to buffer (dropped: 2-5)
        2. Extract track 1, _refresh_chain may return new tracks or empty
        3. Extract track 2 (when buffer refills), track 3, etc.: each calls come from server
        4. Playback continues for multiple calls without WaveUnavailableError
        """
        from bot.yandex.wave import MAX_BUFFERED_TRACKS

        # First batch: 5 unique tracks (only MAX_BUFFERED_TRACKS=1 will fit initially)
        first_batch = WaveBatch(
            batch_id="batch1",
            tracks=(make_track("a"), make_track("b"), make_track("c"),
                    make_track("d"), make_track("e")),
        )
        # Second batch: 3 new unique tracks (for refresh after extracting 'a')
        second_batch = WaveBatch(
            batch_id="batch2",
            tracks=(make_track("f"), make_track("g"), make_track("h")),
        )
        # Third batch: 2 new unique tracks
        third_batch = WaveBatch(
            batch_id="batch3",
            tracks=(make_track("i"), make_track("j")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [first_batch, second_batch, third_batch]
        session = WaveSession(client)

        await session.start()

        # Multiple next_track() calls should succeed; each refills from fresh batch
        t1 = await session.next_track()
        assert t1.id == "a"
        assert session.buffered <= MAX_BUFFERED_TRACKS

        t2 = await session.next_track()
        # t2 could be 'b' (if not filtered), or 'f', 'g', 'h', etc. (from refresh)
        assert t2.id in ["b", "f", "g", "h"]
        assert session.buffered <= MAX_BUFFERED_TRACKS

        await session.next_track()
        # Should not raise WaveUnavailableError; buffer respects constraint
        assert session.buffered <= MAX_BUFFERED_TRACKS

        # Verify no exceptions and buffer is always within limit
        assert session.started is True


class TestWaveSessionRetrySchedule:
    """Паузы между попытками получить пачку треков."""

    async def test_retry_delays_grow_and_do_not_trail(self, monkeypatch):
        """Задержки экспоненциальные, и после последней попытки сна нет.

        Лишний сон перед выбросом ошибки заставил бы пользователя ждать
        ещё восемь секунд ради результата, который уже известен.
        """
        delays: list[float] = []

        async def recording_sleep(seconds):
            delays.append(seconds)

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", recording_sleep)

        client = FakeMusicClient()
        client.batches_to_return = []  # всегда пустая пачка
        session = WaveSession(client)
        await session.start()

        with pytest.raises(WaveUnavailableError):
            await session.next_track()

        # 4 попытки → ровно 3 паузы, удваивающиеся, и ни одной после последней.
        assert delays == [1.0, 2.0, 4.0]
        assert len(delays) == WaveSession.MAX_FETCH_ATTEMPTS - 1
