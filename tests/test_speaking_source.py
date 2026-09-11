"""Тесты говорящего источника: голос бота поверх музыки без второго воспроизведения.

Discord играет в голосовом соединении ровно один источник, поэтому «сказать
фразу поверх музыки» решается подменой на уровне источника. Главное
свойство, которое здесь закрепляется: пока звучит фраза, музыкальный
источник НЕ читается — значит трек не проматывается под голос, а стоит и
продолжается ровно с прерванного места.
"""

from __future__ import annotations

import asyncio

import pytest

from bot.audio.speaking import FRAME_BYTES, SILENCE_FRAME, SpeakingSource

MUSIC_FRAME = b"\x11" * FRAME_BYTES
SPEECH_BYTE = b"\x22"


class FakeMusic:
    """Музыкальный источник-заглушка: считает прочитанные кадры и хранит громкость."""

    def __init__(self) -> None:
        self.reads = 0
        self.volume = 1.0
        self.cleaned = False

    @property
    def elapsed(self) -> float:
        """Позиция трека — ровно по числу выданных кадров, как у настоящего источника."""
        return self.reads * 0.02

    def read(self) -> bytes:
        """Отдаёт узнаваемый кадр, чтобы в тестах было видно, музыка это или речь."""
        self.reads += 1
        return MUSIC_FRAME

    def cleanup(self) -> None:
        """Запоминает, что источник освободили."""
        self.cleaned = True


def _speech(frames: float) -> bytes:
    """Речь заданной длины в кадрах — дробное значение даёт неполный последний кадр."""
    return SPEECH_BYTE * int(FRAME_BYTES * frames)


@pytest.fixture
def loop() -> asyncio.AbstractEventLoop:
    """Цикл событий для выставления события окончания фразы."""
    return asyncio.new_event_loop()


class TestMusicPassthrough:
    """Пока фразы нет, источник прозрачен для музыки."""

    def test_reads_music_when_silent(self, loop):
        """Без фразы отдаётся музыка как есть."""
        music = FakeMusic()
        source = SpeakingSource(music, loop=loop)
        assert source.read() == MUSIC_FRAME
        assert music.reads == 1

    def test_volume_delegates_to_music(self, loop):
        """Громкость читается и пишется в музыкальный источник."""
        music = FakeMusic()
        source = SpeakingSource(music, loop=loop)
        source.volume = 0.3
        assert music.volume == pytest.approx(0.3)
        assert source.volume == pytest.approx(0.3)

    def test_elapsed_delegates_to_music(self, loop):
        """Позиция трека берётся из музыкального источника."""
        music = FakeMusic()
        source = SpeakingSource(music, loop=loop)
        source.read()
        source.read()
        assert source.elapsed == pytest.approx(0.04)

    def test_cleanup_releases_music(self, loop):
        """Освобождение источника освобождает и музыкальный."""
        music = FakeMusic()
        SpeakingSource(music, loop=loop).cleanup()
        assert music.cleaned


class TestSpeechInterrupts:
    """Фраза вклинивается в музыку и приостанавливает её."""

    def test_music_is_not_read_while_speaking(self, loop):
        """Главное свойство: во время фразы музыкальный источник не читается ни разу.

        Если бы читался, трек проматывался бы под голос и после фразы
        продолжился бы не с того места, на котором его прервали.
        """
        music = FakeMusic()
        source = SpeakingSource(music, loop=loop)
        source.read()
        reads_before = music.reads

        source.speak(_speech(3))
        while source.is_speaking:
            source.read()

        assert music.reads == reads_before

    def test_speech_frames_are_returned(self, loop):
        """Во время фразы отдаются именно её байты, а не музыкальные."""
        source = SpeakingSource(FakeMusic(), loop=loop)
        source.speak(_speech(2))
        first = source.read()
        assert set(first) == {SPEECH_BYTE[0]}

    def test_music_resumes_after_speech(self, loop):
        """После фразы музыка продолжается с того же места."""
        music = FakeMusic()
        source = SpeakingSource(music, loop=loop)
        source.speak(_speech(2))
        while source.is_speaking:
            source.read()
        assert source.read() == MUSIC_FRAME

    def test_frames_are_always_full_size(self, loop):
        """Каждый выданный кадр ровно 20 мс — Discord не принимает неполные."""
        source = SpeakingSource(FakeMusic(), loop=loop)
        source.speak(_speech(2.5))
        frames = []
        while source.is_speaking:
            frames.append(source.read())
        assert frames
        assert all(len(frame) == FRAME_BYTES for frame in frames)

    def test_last_frame_padded_with_silence(self, loop):
        """Неполный хвост фразы добивается тишиной, а не отдаётся обрезком.

        Обрезок Discord посчитал бы концом воспроизведения и остановил бы
        музыку вместе с фразой.
        """
        source = SpeakingSource(FakeMusic(), loop=loop)
        source.speak(_speech(1.5))
        frames = []
        while source.is_speaking:
            frames.append(source.read())
        assert frames[-1].endswith(SILENCE_FRAME[: FRAME_BYTES // 2])

    def test_is_speaking_reflects_state(self, loop):
        """Признак «сейчас говорит» включается фразой и гаснет вместе с ней."""
        source = SpeakingSource(FakeMusic(), loop=loop)
        assert not source.is_speaking
        source.speak(_speech(1))
        assert source.is_speaking
        source.read()
        assert not source.is_speaking


class TestWithoutMusic:
    """Источник, существующий только ради фразы."""

    def test_ends_after_speech(self, loop):
        """Без музыки после фразы воспроизводить нечего — источник заканчивается."""
        source = SpeakingSource(None, loop=loop)
        source.speak(_speech(1))
        source.read()
        assert source.read() == b""

    def test_volume_and_elapsed_are_safe(self, loop):
        """Громкость и позиция не падают, когда музыкального источника нет."""
        source = SpeakingSource(None, loop=loop)
        source.volume = 0.5
        assert source.volume == pytest.approx(1.0)
        assert source.elapsed == pytest.approx(0.0)

    def test_cleanup_without_music(self, loop):
        """Освобождение без музыкального источника не падает."""
        SpeakingSource(None, loop=loop).cleanup()


class TestFinishedEvent:
    """Событие окончания фразы — по нему плеер узнаёт, что можно продолжать."""

    @pytest.mark.asyncio
    async def test_event_set_after_last_frame(self):
        """Событие выставляется, когда фраза договорена."""
        source = SpeakingSource(FakeMusic(), loop=asyncio.get_running_loop())
        finished = source.speak(_speech(2))
        while source.is_speaking:
            source.read()
        await asyncio.wait_for(finished.wait(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_empty_speech_finishes_immediately(self):
        """Пустая фраза считается договорённой сразу, а не подвешивает ожидающего."""
        source = SpeakingSource(FakeMusic(), loop=asyncio.get_running_loop())
        assert source.speak(b"").is_set()

    @pytest.mark.asyncio
    async def test_new_speech_releases_previous_waiter(self):
        """Новая фраза заменяет недоговорённую, но не бросает ждавшего прежнюю.

        Иначе тот, кто ждал окончания первой фразы, завис бы навсегда.
        """
        source = SpeakingSource(FakeMusic(), loop=asyncio.get_running_loop())
        first = source.speak(_speech(5))
        source.read()
        second = source.speak(_speech(1))
        assert first.is_set()
        assert not second.is_set()

    @pytest.mark.asyncio
    async def test_cleanup_releases_waiter(self):
        """Освобождение источника снимает ожидание недоговорённой фразы."""
        source = SpeakingSource(FakeMusic(), loop=asyncio.get_running_loop())
        finished = source.speak(_speech(10))
        source.cleanup()
        await asyncio.sleep(0)
        assert finished.is_set()
