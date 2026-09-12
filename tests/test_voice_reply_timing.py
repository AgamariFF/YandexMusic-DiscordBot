"""Тесты синхронизации голосовой реплики и самой голосовой команды в `MusicCog`.

Проверяют требование пользователя: реплика должна начинать звучать сразу,
как только команда распознана, и никогда не должна ждать загрузку модели
синтеза (см. докстрины `MusicCog._speak_voice_reply` и
`MusicCog.execute_voice_command` про замер — 5.5 с загрузки модели, из них
3.2 с полной заморозки цикла событий).

Ког собирается с настоящим `Config` (как в `tests/test_voice_control.py`),
но с подменённым `GuildPlayer` (реальный не подключается к Discord) и
подменённым `TextToSpeech` (реальный грузит модель в 130 МБ — этого тесты
делать не должны). `_dispatch_voice_command` и `_tts.synthesize` в части
тестов заменяются на управляемые заглушки с задержкой — приём для замера
того, что реплика не растягивает время выполнения команды.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from bot.cogs.music import MusicCog
from bot.config import Config
from bot.errors import SpeechSynthesisUnavailableError
from bot.voice_commands import VoiceCommand

# Порог задержки в искусственных заглушках дispatch/synthesize — тот же,
# что и в замере из задачи ("команда с задержкой 0.3 с у каждого занимает
# 0.31 с, а не 0.6").
_STUB_DELAY = 0.3


def _make_config() -> Config:
    """Собирает минимальный конфиг с включёнными синтезом и голосовыми репликами."""
    return Config(
        discord_token="token",
        guild_id=1,
        yandex_token="token",
        log_level="INFO",
        default_volume=1.0,
        ffmpeg_path="ffmpeg",
        idle_timeout=300,
        nekto_token="",
        nekto_user_agent="",
        speech_model_path="models/vosk-model-small-ru-0.22",
        speech_enabled=True,
        speech_transcript=False,
        speech_debug_dir="",
        speech_debug_max_seconds=600.0,
        tts_enabled=True,
        tts_model_name="vosk-model-tts-ru-0.7-multi",
        tts_speaker_id=2,
        voice_replies=True,
    )


def _make_cog(*, tts_ready: bool) -> MusicCog:
    """Собирает `MusicCog` без реального Discord/сети и без реальной модели синтеза.

    `GuildPlayer` подменяется на `AsyncMock` (тот же приём, что и в
    `tests/test_music_commands.py`): реальный конструктор ничего не грузит,
    но реальные методы плеера здесь не нужны вовсе — команда исполняется
    заглушкой `_dispatch_voice_command`. `_tts` подменяется на `MagicMock` с
    управляемым `is_ready`, чтобы не грузить настоящую модель (130 МБ, 5.5
    секунды) ради проверки одной лишь синхронизации по времени.
    """
    mock_player = AsyncMock()
    with patch("bot.cogs.music.GuildPlayer", return_value=mock_player):
        cog = MusicCog(MagicMock(), _make_config(), AsyncMock())
    cog._tts = MagicMock()
    cog._tts.is_ready = tts_ready
    cog._tts.synthesize = AsyncMock(return_value=b"pcm-bytes")
    cog._player.say = AsyncMock()
    cog._update_player_message = AsyncMock()
    return cog


def _slow_stub(delay: float, result: str = "готово"):
    """Асинхронная заглушка с задержкой — для замера, что реплика не тормозит команду."""

    async def _stub(*_args: object, **_kwargs: object) -> str:
        await asyncio.sleep(delay)
        return result

    return _stub


class TestReplyNeverWaitsForModelLoad:
    """Требование 1: неготовая модель не должна тормозить команду и молчит сама."""

    async def test_command_runs_when_model_not_ready(self) -> None:
        """Команда выполняется как обычно, даже если модель синтеза ещё не загружена."""
        cog = _make_cog(tts_ready=False)
        cog._dispatch_voice_command = AsyncMock(return_value="Воспроизведение приостановлено.")

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Воспроизведение приостановлено."
        cog._dispatch_voice_command.assert_awaited_once()

    async def test_synthesize_not_called_when_model_not_ready(self) -> None:
        """При неготовой модели `synthesize` не вызывается вовсе."""
        cog = _make_cog(tts_ready=False)
        cog._dispatch_voice_command = AsyncMock(return_value="Воспроизведение приостановлено.")

        await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        cog._tts.synthesize.assert_not_called()

    async def test_player_say_not_called_when_model_not_ready(self) -> None:
        """При неготовой модели реплика не произносится: `player.say` тоже не вызывается."""
        cog = _make_cog(tts_ready=False)
        cog._dispatch_voice_command = AsyncMock(return_value="Воспроизведение приостановлено.")

        await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        cog._player.say.assert_not_called()


class TestReplyIsSpokenWhenModelReady:
    """Требование 1 (обратная сторона): готовая модель реплику всё-таки произносит."""

    async def test_synthesize_and_say_called_when_ready(self) -> None:
        """При готовой модели `synthesize` и `player.say` вызываются для не-`repeat` действия."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = AsyncMock(return_value="Воспроизведение приостановлено.")

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Воспроизведение приостановлено."
        cog._tts.synthesize.assert_awaited_once()
        cog._player.say.assert_awaited_once_with(b"pcm-bytes")


class TestReplyDoesNotDelayCommand:
    """Требование 2: реплика и команда идут параллельно, а не одна после другой."""

    async def test_total_time_close_to_max_not_sum(self) -> None:
        """При задержках 0.3 с у обеих сторон общее время близко к 0.3 с, а не к 0.6 с."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = _slow_stub(_STUB_DELAY, "Воспроизведение приостановлено.")
        cog._tts.synthesize = AsyncMock(side_effect=_slow_stub(_STUB_DELAY, "pcm"))

        start = time.monotonic()
        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)
        elapsed = time.monotonic() - start

        assert result == "Воспроизведение приостановлено."
        # Сумма двух задержек была бы около 0.6 с — проверяем, что время
        # осталось заметно ближе к максимуму (0.3 с), а не к сумме.
        assert elapsed < _STUB_DELAY * 1.5


class TestStopStaysSequential:
    """Требование: `stop` — единственное исключение, реплика звучит строго до отключения."""

    async def test_reply_finishes_before_dispatch_starts(self) -> None:
        """Прощание успевает договориться (через `player.say`) раньше, чем начнётся отключение."""
        cog = _make_cog(tts_ready=True)
        order: list[str] = []

        async def slow_synthesize(_text: str) -> bytes:
            await asyncio.sleep(_STUB_DELAY)
            order.append("synthesize")
            return b"pcm-bytes"

        async def dispatch_stop(*_args: object, **_kwargs: object) -> str:
            order.append("dispatch")
            return "Отключился от голосового канала."

        cog._tts.synthesize = AsyncMock(side_effect=slow_synthesize)
        cog._dispatch_voice_command = dispatch_stop

        result = await cog.execute_voice_command(VoiceCommand(action="stop"), speaker=None)

        assert result == "Отключился от голосового канала."
        assert order == ["synthesize", "dispatch"]
        cog._player.say.assert_awaited_once_with(b"pcm-bytes")


class TestSynthesisErrorDoesNotBreakCommand:
    """Требование: ошибка реплики проглатывается и не влияет ни на команду, ни на её текст."""

    async def test_command_result_unaffected_by_synthesis_error(self) -> None:
        """Ошибка `synthesize` не мешает команде выполниться и не меняет текст-подтверждение."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = AsyncMock(return_value="Воспроизведение приостановлено.")
        cog._tts.synthesize = AsyncMock(
            side_effect=SpeechSynthesisUnavailableError(
                "модель упала", user_message="не вышло сказать"
            )
        )

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Воспроизведение приостановлено."
        cog._player.say.assert_not_called()
