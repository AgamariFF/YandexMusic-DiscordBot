"""Тесты отладочной записи речи: WAV, расшифровка и счётчик потерь пакетов."""

from __future__ import annotations

import wave
from pathlib import Path

import pytest

from bot.speech_debug import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    PacketLossTracker,
    SpeechRecorder,
)

# Секунда тишины в формате записи — содержимое для проверок неважно, важен объём.
ONE_SECOND = b"\x00\x00" * SAMPLE_RATE


def _sessions_of(root: Path) -> list[Path]:
    """Все каталоги сеансов внутри корня записи, по порядку."""
    return sorted(path for path in root.iterdir() if path.is_dir())


def _session_of(root: Path) -> Path:
    """Единственный каталог сеанса внутри корня записи."""
    sessions = _sessions_of(root)
    assert len(sessions) == 1, sessions
    return sessions[0]


def _read_wav(path: Path) -> tuple[int, int, int, bytes]:
    """Возвращает параметры и содержимое WAV-файла одним чтением."""
    with wave.open(str(path)) as wf:
        return (
            wf.getframerate(),
            wf.getnchannels(),
            wf.getsampwidth(),
            wf.readframes(wf.getnframes()),
        )


class TestPacketLossTracker:
    """Счётчик потерь по разрывам в порядковых номерах пакетов."""

    def test_continuous_sequence_has_no_loss(self):
        """Идущие подряд номера потерями не считаются."""
        tracker = PacketLossTracker()
        for sequence in (10, 11, 12, 13):
            tracker.note(1, sequence)
        assert tracker.stats(1) == (4, 0)

    def test_gap_counts_as_loss(self):
        """Разрыв в номерах — это потерянные по дороге пакеты."""
        tracker = PacketLossTracker()
        tracker.note(1, 100)
        tracker.note(1, 106)
        assert tracker.stats(1) == (2, 5)

    def test_pause_in_speech_is_not_loss(self):
        """Пауза в речи не создаёт разрыва: отправитель просто не шлёт пакетов.

        Это и есть причина, по которой счётчик строится на номерах, а не на
        времени между пакетами: по времени пауза была бы неотличима от
        обрыва связи.
        """
        tracker = PacketLossTracker()
        tracker.note(1, 500)
        tracker.note(1, 501)
        assert tracker.stats(1) == (2, 0)

    def test_sequence_wraparound_is_not_loss(self):
        """Переход через границу 16-битного счётчика — норма, а не 65 тысяч потерь."""
        tracker = PacketLossTracker()
        for sequence in (65534, 65535, 0, 1):
            tracker.note(2, sequence)
        assert tracker.stats(2) == (4, 0)

    def test_implausible_gap_is_ignored(self):
        """Неправдоподобно большой разрыв не записывается в потери."""
        tracker = PacketLossTracker()
        tracker.note(1, 10)
        tracker.note(1, 40000)
        received, lost = tracker.stats(1)
        assert received == 2
        assert lost == 0

    def test_speakers_are_independent(self):
        """Потери одного говорящего не приписываются другому."""
        tracker = PacketLossTracker()
        tracker.note(1, 10)
        tracker.note(2, 900)
        tracker.note(1, 15)
        assert tracker.stats(1) == (2, 4)
        assert tracker.stats(2) == (1, 0)

    def test_drop_speaker_resets_expectation(self):
        """После ухода говорящего его следующий номер не считается разрывом."""
        tracker = PacketLossTracker()
        tracker.note(1, 10)
        tracker.drop_speaker(1)
        tracker.note(1, 5000)
        assert tracker.stats(1)[1] == 0

    def test_report_mentions_each_speaker(self):
        """Отчёт называет каждого говорящего и долю потерь."""
        tracker = PacketLossTracker()
        tracker.note(1, 10)
        tracker.note(1, 12)
        report = tracker.report()
        assert "говорящий 1" in report
        assert "%" in report

    def test_report_without_packets(self):
        """Отчёт без единого пакета не падает и говорит об этом прямо."""
        assert "Ни одного" in PacketLossTracker().report()


class TestSpeechRecorder:
    """Запись звука, расшифровки и итога на диск."""

    @pytest.mark.asyncio
    async def test_writes_audio_verbatim(self, tmp_path: Path):
        """Записанный WAV совпадает с поданным звуком байт в байт.

        Главное свойство записи: на диск обязан попасть ровно тот звук,
        что ушёл в распознавание, иначе по нему нельзя судить, что именно
        слышала модель.
        """
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.write(42, ONE_SECOND)
        await recorder.close()

        rate, channels, width, data = _read_wav(_session_of(tmp_path) / "speaker-42.wav")
        assert rate == SAMPLE_RATE
        assert channels == CHANNELS
        assert width == SAMPLE_WIDTH
        assert data == ONE_SECOND

    @pytest.mark.asyncio
    async def test_separate_file_per_speaker(self, tmp_path: Path):
        """У каждого говорящего свой файл — иначе речь двоих смешалась бы в один."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.write(1, ONE_SECOND)
        recorder.write(2, ONE_SECOND)
        await recorder.close()

        names = {path.name for path in _session_of(tmp_path).iterdir()}
        assert "speaker-1.wav" in names
        assert "speaker-2.wav" in names

    @pytest.mark.asyncio
    async def test_transcript_contains_phrases(self, tmp_path: Path):
        """Расшифровка содержит распознанные фразы с указанием говорящего."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.note_phrase(7, "катя следующий трек")
        await recorder.close()

        transcript = (_session_of(tmp_path) / "transcript.txt").read_text(encoding="utf-8")
        assert "катя следующий трек" in transcript
        assert "7" in transcript

    @pytest.mark.asyncio
    async def test_summary_contains_loss_report(self, tmp_path: Path):
        """Итог содержит статистику потерь — по ней и решается вопрос «модель или связь»."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.loss_tracker.note(5, 100)
        recorder.loss_tracker.note(5, 110)
        await recorder.close()

        summary = (_session_of(tmp_path) / "summary.txt").read_text(encoding="utf-8")
        assert "говорящий 5" in summary
        assert "9" in summary  # девять потерянных пакетов

    @pytest.mark.asyncio
    async def test_respects_size_limit(self, tmp_path: Path):
        """По достижении предела запись говорящего прекращается, а не растёт бесконечно."""
        recorder = SpeechRecorder(str(tmp_path), max_seconds=2)
        await recorder.start()
        for _ in range(10):
            recorder.write(1, ONE_SECOND)
        await recorder.close()

        rate, _, width, data = _read_wav(_session_of(tmp_path) / "speaker-1.wav")
        assert len(data) / width / rate <= 3

    def test_nothing_written_before_start(self, tmp_path: Path):
        """До start() запись ничего не создаёт на диске."""
        recorder = SpeechRecorder(str(tmp_path))
        recorder.write(1, ONE_SECOND)
        recorder.note_phrase(1, "катя пауза")
        assert not list(tmp_path.iterdir())

    @pytest.mark.asyncio
    async def test_start_is_idempotent(self, tmp_path: Path):
        """Повторный start() не заводит второй каталог сеанса."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        first = recorder.session_dir
        await recorder.start()
        assert recorder.session_dir == first
        await recorder.close()
        assert len(_sessions_of(tmp_path)) == 1

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_path: Path):
        """Повторный close() не падает."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.write(1, ONE_SECOND)
        await recorder.close()
        await recorder.close()

    @pytest.mark.asyncio
    async def test_sessions_do_not_mix(self, tmp_path: Path):
        """Разные сеансы записи попадают в разные каталоги."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.write(1, ONE_SECOND)
        first = recorder.session_dir
        await recorder.close()

        await recorder.start()
        second = recorder.session_dir
        await recorder.close()
        assert first != second

    @pytest.mark.asyncio
    async def test_second_session_starts_clean(self, tmp_path: Path):
        """Второй сеанс не наследует от первого ни израсходованный предел, ни расшифровку.

        Без сброса счётчиков сеанс после переподключения к каналу (обычное
        дело) записал бы ноль байт — предел-то уже израсходован — и повторил
        бы в расшифровке фразы прошлого сеанса.
        """
        recorder = SpeechRecorder(str(tmp_path), max_seconds=2)
        await recorder.start()
        recorder.write(1, ONE_SECOND)
        recorder.write(1, ONE_SECOND)
        recorder.note_phrase(1, "фраза первого сеанса")
        await recorder.close()

        await recorder.start()
        recorder.write(1, ONE_SECOND)
        recorder.note_phrase(1, "фраза второго сеанса")
        await recorder.close()

        second = _sessions_of(tmp_path)[1]
        _, _, _, data = _read_wav(second / "speaker-1.wav")
        assert data == ONE_SECOND

        transcript = (second / "transcript.txt").read_text(encoding="utf-8")
        assert "фраза второго сеанса" in transcript
        assert "фраза первого сеанса" not in transcript

    @pytest.mark.asyncio
    async def test_empty_audio_is_ignored(self, tmp_path: Path):
        """Пустой кусок звука не создаёт файла и не падает."""
        recorder = SpeechRecorder(str(tmp_path))
        await recorder.start()
        recorder.write(1, b"")
        await recorder.close()

        assert not sorted(_session_of(tmp_path).glob("*.wav"))
