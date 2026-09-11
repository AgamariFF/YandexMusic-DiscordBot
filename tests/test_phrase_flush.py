"""Тесты досрочного закрытия фразы по паузе — защита от «бот отвечает неизвестно когда».

Фраза сама закрывается только после примерно полусекунды непрерывной
ТИШИНЫ в звуке. Но Discord, когда человек замолчал, шлёт около сотни
миллисекунд тишины и перестаёт слать пакеты вовсе — ожидаемая тишина
просто не приходит. Без досрочного сброса команда становилась известна
лишь тогда, когда человек заговорит СНОВА, то есть неизвестно когда.
Замер на живой фразе: было «текст не появился вовсе», стало 390 мс.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import av
import pytest

from bot import listening
from bot.listening import GuildListener
from bot.nekto.audio import DISCORD_FRAME_SAMPLES, DISCORD_SAMPLE_RATE
from bot.speech import SpeechRecognizer
from bot.voice_commands import VoiceCommand, describe_command

# Короткий таймаут, чтобы тесты не ждали реальные 0.4 с.
FAST_TIMEOUT = 0.05


def _silent_frame() -> av.AudioFrame:
    """Кадр тишины в формате Discord — содержимое неважно, важен сам факт кадра."""
    frame = av.AudioFrame(format="s16", layout="stereo", samples=DISCORD_FRAME_SAMPLES)
    frame.sample_rate = DISCORD_SAMPLE_RATE
    return frame


class _FakeRecognizer:
    """Распознавание-заглушка: `feed` молчит, `flush` отдаёт заготовленный текст."""

    def __init__(self, flush_text: str | None = "катя пауза") -> None:
        self.flush_text = flush_text
        self.fed = 0
        self.flushes = 0

    async def ensure_ready(self) -> None:
        return None

    async def feed(self, speaker_id: int, pcm: bytes) -> str | None:
        self.fed += 1
        return None

    async def flush(self, speaker_id: int) -> str | None:
        self.flushes += 1
        text, self.flush_text = self.flush_text, None
        return text

    def drop_speaker(self, speaker_id: int) -> None:
        return None


async def _pump_listener(listener: GuildListener, frames: int = 1) -> None:
    """Кладёт кадры в очередь говорящего и даёт фоновой задаче отработать."""
    listener._loop = asyncio.get_running_loop()
    for _ in range(frames):
        listener._enqueue_frame(1, _silent_frame())
    await asyncio.sleep(0)


class TestFlushOnPause:
    """Досрочный сброс фразы, когда пакеты от говорящего прекратились."""

    @pytest.mark.asyncio
    async def test_phrase_delivered_after_pause(self, monkeypatch):
        """Команда доходит до колбэка после паузы, а не после следующей реплики."""
        monkeypatch.setattr(listening, "PHRASE_SILENCE_TIMEOUT", FAST_TIMEOUT)
        got: list[str] = []

        async def on_phrase(speaker_id: int, text: str) -> None:
            got.append(text)

        recognizer = _FakeRecognizer("катя пауза")
        listener = GuildListener(recognizer=recognizer, on_phrase=on_phrase)
        await _pump_listener(listener)
        await asyncio.sleep(FAST_TIMEOUT * 4)

        assert got == ["катя пауза"]
        for task in listener._pump_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_no_flush_without_audio(self, monkeypatch):
        """Пока говорящий молчит, пустые сбросы не идут — иначе это была бы холостая работа."""
        monkeypatch.setattr(listening, "PHRASE_SILENCE_TIMEOUT", FAST_TIMEOUT)
        recognizer = _FakeRecognizer(None)
        listener = GuildListener(recognizer=recognizer)
        await _pump_listener(listener)
        await asyncio.sleep(FAST_TIMEOUT * 5)

        # Ровно один сброс на одну порцию речи, а не по одному на таймаут.
        assert recognizer.flushes == 1
        for task in listener._pump_tasks.values():
            task.cancel()

    @pytest.mark.asyncio
    async def test_new_audio_starts_new_phrase(self, monkeypatch):
        """После сброса новая речь снова подлежит сбросу по следующей паузе."""
        monkeypatch.setattr(listening, "PHRASE_SILENCE_TIMEOUT", FAST_TIMEOUT)
        recognizer = _FakeRecognizer(None)
        listener = GuildListener(recognizer=recognizer)
        await _pump_listener(listener)
        await asyncio.sleep(FAST_TIMEOUT * 3)
        await _pump_listener(listener)
        await asyncio.sleep(FAST_TIMEOUT * 3)

        assert recognizer.flushes == 2
        for task in listener._pump_tasks.values():
            task.cancel()


class TestRecognizerFlush:
    """Сам метод `SpeechRecognizer.flush`."""

    @pytest.mark.asyncio
    async def test_unknown_speaker_returns_none(self):
        """Сброс несуществующего говорящего не падает и возвращает None."""
        assert await SpeechRecognizer().flush(12345) is None

    @pytest.mark.asyncio
    async def test_returns_final_text(self, monkeypatch):
        """Сброс отдаёт текст незакрытой фразы."""
        recognizer = SpeechRecognizer()
        state = MagicMock()
        state.lock = asyncio.Lock()
        state.recognizer.FinalResult.return_value = '{"text": "катя следующий"}'
        recognizer._speakers[1] = state
        assert await recognizer.flush(1) == "катя следующий"

    @pytest.mark.asyncio
    async def test_empty_text_becomes_none(self, monkeypatch):
        """Пустой результат сброса превращается в None, а не в пустую строку."""
        recognizer = SpeechRecognizer()
        state = MagicMock()
        state.lock = asyncio.Lock()
        state.recognizer.FinalResult.return_value = '{"text": "   "}'
        recognizer._speakers[1] = state
        assert await recognizer.flush(1) is None


class TestDescribeCommand:
    """Короткое описание команды для мгновенного подтверждения в чате."""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (VoiceCommand(action="pause"), "пауз"),
            (VoiceCommand(action="skip"), "трек"),
            (VoiceCommand(action="stop"), "отключа"),
            (VoiceCommand(action="wave"), "волн"),
            (VoiceCommand(action="search", query="кино"), "кино"),
            (VoiceCommand(action="volume", volume_percent=40), "40"),
            (VoiceCommand(action="volume", volume_delta=20), "громче"),
            (VoiceCommand(action="volume", volume_delta=-20), "тише"),
        ],
    )
    def test_description_mentions_the_point(self, command, expected):
        """Описание называет суть команды — по нему человек узнаёт, верно ли его поняли."""
        assert expected in describe_command(command)

    def test_search_query_is_included(self):
        """Поисковый запрос попадает в описание: иначе не видно, что именно расслышал бот."""
        described = describe_command(VoiceCommand(action="wave", query="группа кино"))
        assert "группа кино" in described

    def test_every_action_has_description(self):
        """У каждого действия есть непустое описание — без заглушки «выполняю команду»."""
        actions = ("pause", "resume", "skip", "stop", "wave", "search", "volume", "now_playing")
        for action in actions:
            described = describe_command(VoiceCommand(action=action))
            assert described and described != "выполняю команду"
