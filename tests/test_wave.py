"""Tests for bot.yandex.wave module."""


import pytest

from bot.errors import WaveUnavailableError, YandexAuthError
from bot.yandex.client import TrackInfo, WaveBatch
from bot.yandex.wave import WaveSession


class FakeMusicClient:
    """Поддельный YandexMusicClient для тестирования.

    Поддерживает как список пачек (для простых случаев), так и функцию-обработчик
    (для сложных сценариев с ретраями и ошибками).
    """

    def __init__(self):
        self.calls = []
        self.batches_to_return = []
        self.batch_index = 0
        self.fetch_handler = None

    async def start_session(self) -> WaveBatch:
        """Создаёт сессию и возвращает первую пачку треков."""
        self.calls.append(("start_session",))
        if self.batch_index < len(self.batches_to_return):
            batch = self.batches_to_return[self.batch_index]
            self.batch_index += 1
            return batch
        return WaveBatch(batch_id=None, tracks=())

    async def fetch_session_tracks(
        self, *, queue: list[str], feedbacks: list[dict]
    ) -> WaveBatch:
        """Запрашивает следующую пачку треков с фидбеком."""
        self.calls.append(("fetch_session_tracks", queue, feedbacks))
        if self.fetch_handler is not None:
            return await self.fetch_handler(queue, self.batch_index, self)
        if self.batch_index < len(self.batches_to_return):
            batch = self.batches_to_return[self.batch_index]
            self.batch_index += 1
            return batch
        return WaveBatch(batch_id=None, tracks=())

    async def send_feedbacks(self, feedbacks: list[dict]) -> None:
        """Отправляет пачку фидбеков."""
        self.calls.append(("send_feedbacks", feedbacks))


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
    """Тесты метода start()."""

    @pytest.mark.asyncio
    async def test_start_marks_started(self):
        """start() устанавливает started=True."""
        batch = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        assert session.started is True

    @pytest.mark.asyncio
    async def test_start_calls_client_start_session(self):
        """start() вызывает client.start_session()."""
        batch = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        assert any(call[0] == "start_session" for call in client.calls)

    @pytest.mark.asyncio
    async def test_start_sends_pending_feedbacks_before_creating_session(self):
        """start() отправляет накопленный фидбек перед созданием новой сессии.

        Это защита от регрессии: при перезапуске волны поверх играющего трека
        скип успевает уйти по старой сессии.
        """
        batch = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        client = FakeMusicClient()
        client.batches_to_return = [batch, batch]
        session = WaveSession(client)

        # Первый start() заполнит буфер
        await session.start()
        track = await session.next_track()

        # Пропустим трек, что добавит фидбек в очередь
        await session.track_skipped(track, 5.0)

        # Второй start() должен отправить этот фидбек перед start_session
        await session.start()

        # Проверяем порядок вызовов: send_feedbacks должен быть перед start_session
        send_feedbacks_index = None
        start_session_index = None
        for i, call in enumerate(client.calls):
            if call[0] == "send_feedbacks":
                send_feedbacks_index = i
            elif call[0] == "start_session":
                start_session_index = i

        assert send_feedbacks_index is not None
        assert start_session_index is not None
        assert send_feedbacks_index < start_session_index


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
        """next_track() возвращает треки по порядку; буфер не превышает MAX_BUFFERED_TRACKS."""
        from bot.yandex.wave import MAX_BUFFERED_TRACKS

        track1 = make_track("1", "Song1")
        track2 = make_track("2", "Song2")
        track3 = make_track("3", "Song3")
        batch1 = WaveBatch(batch_id="batch1", tracks=(track1, track2))
        batch2 = WaveBatch(batch_id="batch2", tracks=(track3,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()

        t1 = await session.next_track()
        assert t1.id == "1"
        assert session.buffered <= MAX_BUFFERED_TRACKS, (
            f"После первого next_track() буфер превышает "
            f"MAX_BUFFERED_TRACKS={MAX_BUFFERED_TRACKS}, получено {session.buffered}"
        )

        await session.next_track()
        assert session.buffered <= MAX_BUFFERED_TRACKS, (
            f"После второго next_track() буфер превышает "
            f"MAX_BUFFERED_TRACKS={MAX_BUFFERED_TRACKS}, получено {session.buffered}"
        )

    @pytest.mark.asyncio
    async def test_next_track_fetches_new_batch_when_empty(self):
        """next_track() запрашивает новую пачку при пустом буфере и обновляет цепочку.

        Каждый next_track():
        1. Запрашивает пачку для заполнения буфера (если пуст)
        2. Извлекает трек из буфера
        3. Вызывает _refresh_chain() который запрашивает ещё одну пачку

        Проверяем, что происходит не менее одного запроса на next_track().
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

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        assert len(fetch_calls) >= 2

    @pytest.mark.asyncio
    async def test_next_track_passes_last_track_id_as_queue(self):
        """next_track() передаёт последний трек как параметр queue."""
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))
        batch3 = WaveBatch(batch_id="batch3", tracks=(make_track("3"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2, batch3]
        session = WaveSession(client)

        await session.start()
        await session.next_track()
        await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        # Первый fetch_calls[0] - это _refresh_chain после первого next_track() с queue=["1"]
        # Второй fetch_calls[1] - это заполнение буфера для второго next_track() с queue=[]
        # Третий fetch_calls[2] - это _refresh_chain после второго next_track() с queue=["2"]
        assert len(fetch_calls) >= 2
        assert fetch_calls[0][1] == ["1"]

    @pytest.mark.asyncio
    async def test_all_empty_batches_raise_error(self, monkeypatch):
        """Все пустые пачки → WaveUnavailableError."""

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

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        assert len(fetch_calls) == WaveSession.MAX_FETCH_ATTEMPTS

    @pytest.mark.asyncio
    async def test_retry_on_empty_then_success(self):
        """Первая пачка пуста, вторая содержит трек → успех с ретраями и обновлением.

        Сценарий:
        1. Первый запрос возвращает пусто → ретрай
        2. Второй запрос возвращает трек → успех, заполнить буфер
        3. Извлечь трек, вызвать _refresh_chain который делает ещё один запрос
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
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        assert len(fetch_calls) >= 2


class TestWaveSessionUpcoming:
    """Тесты метода upcoming()."""

    @pytest.mark.asyncio
    async def test_upcoming_returns_preview(self):
        """upcoming() возвращает буферированные треки без их модификации."""
        batch = WaveBatch(
            batch_id="b1",
            tracks=(make_track("1"), make_track("2"), make_track("3")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        await session.next_track()

        preview = session.upcoming(2)

        from bot.yandex.wave import MAX_BUFFERED_TRACKS
        assert len(preview) <= MAX_BUFFERED_TRACKS
        buffered_before = session.buffered
        session.upcoming(5)
        assert session.buffered == buffered_before

    @pytest.mark.asyncio
    async def test_upcoming_zero_limit(self):
        """upcoming(0) возвращает пустой список."""
        batch = WaveBatch(batch_id="b1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        preview = session.upcoming(0)

        assert preview == []

    @pytest.mark.asyncio
    async def test_upcoming_negative_limit(self):
        """upcoming(-1) возвращает пустой список."""
        batch = WaveBatch(batch_id="b1", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        preview = session.upcoming(-1)

        assert preview == []

    @pytest.mark.asyncio
    async def test_upcoming_limit_exceeds_buffer(self):
        """upcoming(100) возвращает не более чем буферировано."""
        batch = WaveBatch(
            batch_id="b1",
            tracks=(make_track("1"), make_track("2"), make_track("3")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        await session.next_track()

        preview = session.upcoming(100)

        from bot.yandex.wave import MAX_BUFFERED_TRACKS
        assert len(preview) <= MAX_BUFFERED_TRACKS


class TestWaveSessionFeedback:
    """Тесты методов track_started/finished/skipped."""

    @pytest.mark.asyncio
    async def test_track_started_enqueues_feedback(self):
        """track_started() кладёт фидбек в очередь."""
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()
        await session.track_started(track)

        await session.next_track()
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        assert len(fetch_calls) >= 2
        assert len(fetch_calls[-1][2]) > 0

    @pytest.mark.asyncio
    async def test_track_finished_normalizes_played_seconds(self):
        """track_finished() нормализует отрицательные played_seconds в 0."""
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()
        await session.track_finished(track, -5.0)

        await session.next_track()
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        feedbacks = fetch_calls[-1][2]
        assert len(feedbacks) > 0
        assert feedbacks[0]["event"]["totalPlayedSeconds"] == 0.0

    @pytest.mark.asyncio
    async def test_track_skipped_normalizes_played_seconds(self):
        """track_skipped() нормализует played_seconds."""
        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch2]
        session = WaveSession(client)

        await session.start()
        track = await session.next_track()
        await session.track_skipped(track, -5.0)

        await session.next_track()
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        feedbacks = fetch_calls[-1][2]
        assert len(feedbacks) > 0
        assert feedbacks[0]["event"]["totalPlayedSeconds"] == 0.0


class TestWaveSessionBatchId:
    """Тесты свойства batch_id."""

    @pytest.mark.asyncio
    async def test_batch_id_from_fetch(self):
        """batch_id приходит из полученной пачки."""
        batch = WaveBatch(batch_id="custom-batch-123", tracks=(make_track("1"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()
        # После start() batch_id устанавливается из start_session()
        assert session.batch_id == "custom-batch-123"

    @pytest.mark.asyncio
    async def test_batch_id_preserved_across_empty_batch_retry(self, monkeypatch):
        """batch_id не перезаписывается пустой пачкой при ретрае.

        Регрессионный тест: при next_track(), когда получена пустая пачка,
        _batch_id НЕ должна обновляться в None. Обновление должно происходить
        только при непустой пачке.

        Сценарий (для MAX_BUFFERED_TRACKS=1):
        1. Первая пачка имеет batch_id="batch-1" с 1 треком
        2. Извлечь трек, _refresh_chain возвращает пусто (без обновления)
        3. Следующий fetch для заполнения буфера возвращает пусто (ретрай)
        4. Финальный ретрай возвращает batch_id="batch-2" с 1 треком
        5. batch_id никогда не должна быть None
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        batch1 = WaveBatch(batch_id="batch-1", tracks=(make_track("1"),))
        batch_empty = WaveBatch(batch_id=None, tracks=())
        batch2 = WaveBatch(batch_id="batch-2", tracks=(make_track("3"),))
        batch3 = WaveBatch(batch_id="batch-3", tracks=(make_track("4"),))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch_empty, batch_empty, batch2, batch3]
        session = WaveSession(client)

        await session.start()

        track1 = await session.next_track()
        assert track1.id == "1"
        assert session.batch_id == "batch-1"

        track2 = await session.next_track()
        assert track2.id == "3"
        assert session.batch_id == "batch-2"

        await session.track_started(track2)

        await session.next_track()
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        feedbacks = fetch_calls[-1][2]
        assert len(feedbacks) > 0
        assert feedbacks[0]["batchId"] == "batch-2"


class TestWaveSessionSkipAndAdaptation:
    """Тесты адаптации волны при пропуске трека и дедупликации."""

    @pytest.mark.asyncio
    async def test_skip_keeps_queue_intact(self):
        """track_skipped() НЕ очищает буфер — только кладёт фидбек в очередь.

        Регрессионный тест: /skip должен работать как next_track, а не переибирать очередь.
        После пропуска трека буфер должен сохранить существующие треки.

        Ключ: track_skipped() кладёт фидбек в очередь, но НЕ очищает self._buffer.
        Буфер обновляется только _refresh_chain() (после каждого next_track).
        """
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")

        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                return WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()

        taken = await session.next_track()
        assert taken.id == "a"
        buffered_before_skip = session.buffered

        await session.track_skipped(taken, 10.0)

        assert session.buffered == buffered_before_skip

    @pytest.mark.asyncio
    async def test_skip_feedback_sent_with_next_fetch(self):
        """Фидбек пропуска отправляется вместе со следующим запросом пачки.

        Когда track_skipped() вызывается, фидбек кладётся в очередь. Следующий
        next_track() будет отправлять этот фидбек ВМЕСТЕ с запросом следующей пачки
        (через _refresh_chain/fetch_session_tracks), а не отдельным запросом.

        Это критично: если бы фидбек отправлялся отдельно, возможна гонка
        между ним и получением новой пачки.
        """
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

        await session.track_skipped(taken, 5.0)
        await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        send_feedbacks_calls = [c for c in client.calls if c[0] == "send_feedbacks"]

        found_skip_feedback = False
        for call in fetch_calls:
            for feedback in call[2]:
                if feedback["event"]["type"] == "skip":
                    found_skip_feedback = True
                    break

        assert found_skip_feedback, (
            "Фидбек пропуска должен быть отправлен с fetch_session_tracks"
        )

        non_empty_send_feedbacks = [
            c for c in send_feedbacks_calls if len(c[1]) > 0
        ]
        assert len(non_empty_send_feedbacks) == 0, (
            "send_feedbacks с непустым списком не должен быть вызван: "
            "фидбек должен уйти с fetch_session_tracks"
        )

    @pytest.mark.asyncio
    async def test_next_track_after_skip_passes_track_id_to_refresh(self, monkeypatch):
        """После next_track(), _refresh_chain использует id трека как параметр queue.

        Ключевое поведение: когда next_track() извлекает трек, вызывается
        _refresh_chain(track.id), которая отправляет fetch_session_tracks(queue=[track.id]).

        Сценарий (с MAX_BUFFERED_TRACKS=1):
        1. Первый next_track(): fetch возвращает A, extract A,
           _refresh_chain вызывает fetch(queue=["a"])
        2. Пропустить трек A
        3. Второй next_track(): fetch возвращает B, extract B,
           _refresh_chain вызывает fetch(queue=["b"])
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")

        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                return WaveBatch(batch_id="batch1", tracks=(track_a,))
            elif fetch_count["count"] == 2:
                return WaveBatch(batch_id="batch1-refresh", tracks=(track_b,))
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()

        taken_a = await session.next_track()
        assert taken_a.id == "a"

        await session.track_skipped(taken_a, 5.0)

        taken_b = await session.next_track()
        assert taken_b.id == "b"

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        assert len(fetch_calls) >= 3, f"Ожидается >=3 запросов, получено {len(fetch_calls)}"
        # Проверяем, что queue параметры в правильном порядке
        assert fetch_calls[0][1] == []  # Начальное заполнение (queue пуст, т.к. no last_track)
        assert fetch_calls[1][1] == ["a"]
        assert fetch_calls[-1][1] == ["b"], (
            f"Последний fetch должен иметь queue=['b'] "
            f"(id трека из второго next_track()), получено queue={fetch_calls[-1][1]!r}"
        )

    @pytest.mark.asyncio
    async def test_duplicates_filtered_from_new_batch(self):
        """Недавно игравшие треки отфильтровываются из новых пачек (_refresh_chain).

        Сценарий:
        1. next_track() получает трек A из batch1 (A, B), вызывает _refresh_chain(A)
        2. _refresh_chain запрашивает batch2 (A, C) — A недавний, отфильтрован → buffer=[C]
        3. next_track() извлекает C из буфера (заполненного _refresh_chain)
        """
        track_a = make_track("a", "Track A")
        track_b = make_track("b", "Track B")
        track_c = make_track("c", "Track C")

        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            if fetch_count["count"] == 1:
                return WaveBatch(batch_id="batch1", tracks=(track_a, track_b))
            elif fetch_count["count"] == 2:
                return WaveBatch(batch_id="batch2", tracks=(track_a, track_c))
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()

        t1 = await session.next_track()
        assert t1.id == "a"

        t2 = await session.next_track()
        assert t2.id == "c", (
            f"Ожидается трек C (новый трек после фильтрации дубликата A), получено {t2.id}."
        )

    @pytest.mark.asyncio
    async def test_all_duplicates_fallback_to_unfiltered(self, monkeypatch):
        """Если пачка содержит только недавние треки и это последняя попытка, используй без фильтра.

        Защита от бесконечного цикла: если все треки в пачке недавние
        и MAX_FETCH_ATTEMPTS исчерпаны, верни трек в любом случае (даже дубликат)
        вместо ошибки. Музыка не должна остановиться из-за логики дедупликации.
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a", "Track A")

        batch1 = WaveBatch(batch_id="batch1", tracks=(track_a,))
        batch_dup = WaveBatch(batch_id="batch-dup", tracks=(track_a,))

        client = FakeMusicClient()
        client.batches_to_return = [batch1, batch_dup, batch_dup, batch_dup, batch_dup]
        session = WaveSession(client)

        await session.start()

        t1 = await session.next_track()
        assert t1.id == "a"

        # Буфер пуст (наполненный _refresh_chain пуст т.к. все треки дубликаты)
        # Следующий fetch возвращает только A (недавний)
        # На попытках 1-3: отфильтрован, ретрай
        # На попытке 4 (MAX_FETCH_ATTEMPTS): фильтрован но нет дальше ретраев, используй без фильтра
        # Результат: должен вернуть A без WaveUnavailableError
        t2 = await session.next_track()
        assert t2.id == "a", (
            "Должен вернуть дубликат трека A как запасной вариант "
            "когда все треки в финальной пачке дубликаты (лучше повтор чем остановка)"
        )

    @pytest.mark.asyncio
    async def test_start_clears_history(self):
        """start() очищает историю недавних треков.

        После использования трека A в сессии 1, вызов start() должен очистить
        _recent. Во второй сессии новая пачка с A должна вернуть A
        (нет дедупликации через границы перезапуска).
        """
        track_a = make_track("a", "Track A")

        fetch_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            fetch_count["count"] += 1
            return WaveBatch(batch_id=f"batch{fetch_count['count']}", tracks=(track_a,))

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()
        t1 = await session.next_track()
        assert t1.id == "a"

        await session.start()

        t2 = await session.next_track()
        assert t2.id == "a", (
            "После перезапуска история должна быть очищена; "
            "ранее игравшийся трек A должен быть опять воспроизводим"
        )

    @pytest.mark.asyncio
    async def test_duplicates_then_empty_batch_still_plays(self, monkeypatch):
        """Повторы на ранних попытках не теряются, если последняя пачка пустая.

        Сценарий: попытки 1-2 возвращают непустые пачки из одних недавних треков,
        а попытка 3 приходит пустой. Играбельные треки были получены, поэтому
        музыка обязана продолжиться повтором, а не умереть с WaveUnavailableError.
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

        client.batches_to_return = [
            WaveBatch(batch_id="b1", tracks=(track_a,)),
            WaveBatch(batch_id="b2", tracks=(track_a,)),
            WaveBatch(batch_id="b3", tracks=()),
        ]
        client.batch_index = 0

        track = await session.next_track()
        assert track.id == "a"


class TestWaveSessionNetworkResilience:
    """Тесты обработки сетевых ошибок и ретраев."""

    @pytest.mark.asyncio
    async def test_network_error_retried_then_succeeds(self, monkeypatch):
        """Сетевой сбой на попытках 1-2 не убивает волну: ретрай → успех на попытке 3."""
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        track_a = make_track("a")
        attempt_count = {"count": 0}

        async def fetch_handler_with_retry(queue, batch_index, client):
            attempt_count["count"] += 1
            if attempt_count["count"] <= 2:
                raise WaveUnavailableError(user_message="Network error (simulated)")
            return WaveBatch(batch_id="batch_a", tracks=(track_a,))

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_with_retry
        session = WaveSession(client)

        await session.start()

        track = await session.next_track()
        assert track.id == "a"
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
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

        with pytest.raises(WaveUnavailableError):
            await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
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

        with pytest.raises(YandexAuthError):
            await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        assert len(fetch_calls) == 1

    @pytest.mark.asyncio
    async def test_refresh_chain_error_does_not_stop_playback(self):
        """Ошибка _refresh_chain не прерывает воспроизведение.

        Сценарий (с MAX_BUFFERED_TRACKS=1):
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
                return WaveBatch(batch_id="batch1", tracks=(track_a,))
            else:
                raise WaveUnavailableError(user_message="Refresh failed")

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_refresh_fails
        session = WaveSession(client)

        await session.start()

        track = await session.next_track()
        assert track.id == "a"
        assert session.buffered == 0
        assert session.started is True

    @pytest.mark.asyncio
    async def test_feedbacks_preserved_on_fetch_error_and_resent_in_order(self, monkeypatch):
        """Фидбеки сохраняются при ошибке fetch и отправляются в исходном порядке.

        При сбое fetch_session_tracks фидбеки возвращаются в начало очереди,
        сохраняя исходный порядок (через extendleft с reversed), и уходят
        со следующей успешной попытки. Одно нечётное падение обеспечивает
        видимость одного разворота: без reversed тест упадёт.
        """
        async def fake_sleep(_):
            return None

        monkeypatch.setattr("bot.yandex.wave.asyncio.sleep", fake_sleep)

        batch1 = WaveBatch(batch_id="batch1", tracks=(make_track("1"),))
        batch2 = WaveBatch(batch_id="batch2", tracks=(make_track("2"),))

        attempt_count = {"count": 0}

        async def fetch_handler(queue, batch_index, client):
            attempt_count["count"] += 1
            if attempt_count["count"] == 1:
                return batch1
            if attempt_count["count"] == 2:
                return WaveBatch(batch_id=None, tracks=())
            if attempt_count["count"] == 3:
                raise WaveUnavailableError(user_message="Network error")
            if attempt_count["count"] == 4:
                return batch2
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler
        session = WaveSession(client)

        await session.start()
        track1 = await session.next_track()

        await session.track_started(track1)
        await session.track_finished(track1, 42.5)
        await session.track_skipped(track1, 10.0)

        await session.next_track()

        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        feedback_calls = [c for c in fetch_calls if len(c[2]) == 3]
        assert len(feedback_calls) >= 2, (
            "Должно быть минимум 2 попытки отправить фидбеки "
            "(первая падает, вторая успешна)"
        )

        successfully_sent = feedback_calls[-1]
        feedbacks = successfully_sent[2]

        types = [f["event"]["type"] for f in feedbacks]
        assert types == ["trackStarted", "trackFinished", "skip"], (
            f"Фидбеки после ошибки должны быть в исходном порядке "
            f"(trackStarted, trackFinished, skip), получено {types}"
        )


class TestWaveSessionBatchIdProperty:
    """Тесты семантики свойства batch_id."""

    @pytest.mark.asyncio
    async def test_batch_id_is_from_extracted_track(self):
        """batch_id возвращает идентификатор пачки ПОСЛЕДНЕГО ВЫДАННОГО трека.

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
                return WaveBatch(batch_id="original", tracks=(track_a, track_b))
            elif fetch_count["count"] == 2:
                return WaveBatch(batch_id="refreshed", tracks=(make_track("c"),))
            return WaveBatch(batch_id=None, tracks=())

        client = FakeMusicClient()
        client.fetch_handler = fetch_handler_different_batches
        session = WaveSession(client)

        await session.start()

        track = await session.next_track()
        assert track.id == "a"

        assert session.batch_id == "original", (
            f"batch_id должен быть 'original' (из извлечённого трека), "
            f"а не 'refreshed' (из _refresh_chain), получено {session.batch_id!r}"
        )

        await session.track_skipped(track, 5.0)

        await session.next_track()
        fetch_calls = [c for c in client.calls if c[0] == "fetch_session_tracks"]
        feedbacks = fetch_calls[-1][2]
        assert len(feedbacks) > 0
        assert feedbacks[0]["batchId"] == "original", (
            f"track_skipped должен использовать batch_id='original', "
            f"получено {feedbacks[0]['batchId']!r}"
        )


class TestWaveSessionBufferingConstraints:
    """Тесты ограничений размера буфера."""

    @pytest.mark.asyncio
    async def test_buffer_never_exceeds_one_track(self):
        """Сервер возвращает 5 треков → буфер хранит максимум MAX_BUFFERED_TRACKS."""
        from bot.yandex.wave import MAX_BUFFERED_TRACKS

        track1 = make_track("1")
        track2 = make_track("2")
        track3 = make_track("3")
        track4 = make_track("4")
        track5 = make_track("5")

        batch = WaveBatch(
            batch_id="batch1",
            tracks=(track1, track2, track3, track4, track5),
        )

        client = FakeMusicClient()
        client.batches_to_return = [batch]
        session = WaveSession(client)

        await session.start()

        t1 = await session.next_track()
        assert t1.id == "1"
        assert session.buffered <= MAX_BUFFERED_TRACKS, (
            f"После извлечения из 5-трекового батча, буфер превышает "
            f"MAX_BUFFERED_TRACKS={MAX_BUFFERED_TRACKS}, получено {session.buffered}. "
            "Ограничение должно быть применено при загрузке!"
        )

    @pytest.mark.asyncio
    async def test_dropped_chain_tracks_are_refetched(self):
        """Лишние треки из большой пачки не теряются; next_track() продолжает без ошибок.

        Сценарий:
        1. Сервер возвращает пачку с 5 новыми треками → только 1 в буфер (остальные: 2-5)
        2. Извлечь трек 1, _refresh_chain может вернуть новые треки или пусто
        3. Извлечь трек 2 (когда буфер пополнится), трек 3, etc: каждый запрашивается сервера
        4. Воспроизведение продолжается несколько вызовов без WaveUnavailableError
        """
        from bot.yandex.wave import MAX_BUFFERED_TRACKS

        first_batch = WaveBatch(
            batch_id="batch1",
            tracks=(make_track("a"), make_track("b"), make_track("c"),
                    make_track("d"), make_track("e")),
        )
        second_batch = WaveBatch(
            batch_id="batch2",
            tracks=(make_track("f"), make_track("g"), make_track("h")),
        )
        third_batch = WaveBatch(
            batch_id="batch3",
            tracks=(make_track("i"), make_track("j")),
        )

        client = FakeMusicClient()
        client.batches_to_return = [first_batch, second_batch, third_batch]
        session = WaveSession(client)

        await session.start()

        t1 = await session.next_track()
        assert t1.id == "a"
        assert session.buffered <= MAX_BUFFERED_TRACKS

        t2 = await session.next_track()
        assert t2.id in ["b", "f", "g", "h"]
        assert session.buffered <= MAX_BUFFERED_TRACKS

        await session.next_track()
        assert session.buffered <= MAX_BUFFERED_TRACKS

        assert session.started is True


class TestWaveSessionRetrySchedule:
    """Паузы между попытками получить пачку треков."""

    @pytest.mark.asyncio
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
        client.batches_to_return = []
        session = WaveSession(client)
        await session.start()

        with pytest.raises(WaveUnavailableError):
            await session.next_track()

        assert delays == [1.0, 2.0, 4.0]
        assert len(delays) == WaveSession.MAX_FETCH_ATTEMPTS - 1
