"""Тесты голосового озвучивания ошибок и пустого результата поиска у голосовых команд.

Проверяют требование пользователя: если голосовая команда упала с
`BotError`, бот произносит вслух короткую вводную фразу («Не вышло» и т.п.)
и следом причину из `exc.user_message` — раньше причина уходила только
текстом в чат, и человек, отдавший команду голосом, о ней не узнавал. То же
самое для пустого результата поиска голосовой командой: он не является
`BotError` (это штатный исход, а не ошибка), но человек всё равно должен
услышать об этом, а не читать чат.

Ког собирается с настоящим `Config` (как в `tests/test_voice_control.py`), но
с подменённым `GuildPlayer` (реальный не подключается к Discord, приём — как
в `tests/test_voice_reply_timing.py`) и подменённым `TextToSpeech` (реальный
грузит модель в 130 МБ — этого тесты делать не должны).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bot.cogs.music import MusicCog
from bot.config import Config
from bot.errors import BotError, SearchUnavailableError, SpeechSynthesisUnavailableError
from bot.voice_commands import VoiceCommand


def _make_config(*, voice_replies: bool = True) -> Config:
    """Собирает минимальный конфиг с включённым синтезом и настраиваемыми репликами."""
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
        voice_replies=voice_replies,
    )


def _make_cog(*, tts_ready: bool = True, voice_replies: bool = True) -> MusicCog:
    """Собирает `MusicCog` без реального Discord/сети и без реальной модели синтеза.

    `GuildPlayer` подменяется на `AsyncMock` (тот же приём, что и в
    `tests/test_voice_reply_timing.py`): реальный конструктор ничего не
    грузит, но реальные методы плеера здесь не нужны. `_tts` подменяется на
    `MagicMock` с управляемым `is_ready`, чтобы не грузить настоящую модель.
    """
    mock_player = AsyncMock()
    with patch("bot.cogs.music.GuildPlayer", return_value=mock_player):
        cog = MusicCog(MagicMock(), _make_config(voice_replies=voice_replies), AsyncMock())
    cog._tts = MagicMock()
    cog._tts.is_ready = tts_ready
    cog._tts.synthesize = AsyncMock(return_value=b"pcm-bytes")
    cog._player.say = AsyncMock()
    cog._update_player_message = AsyncMock()
    return cog


class TestBotErrorSpokenAloud:
    """Требование 1: ошибка действия озвучивается вслух и возвращается текстом."""

    async def test_speaks_phrase_containing_user_message_and_returns_it(self) -> None:
        """Бот произносит фразу с причиной ошибки и возвращает тот же текст в чат."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = AsyncMock(
            side_effect=SearchUnavailableError(user_message="Поиск треков сейчас недоступен.")
        )

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Поиск треков сейчас недоступен."
        cog._player.say.assert_awaited_with(b"pcm-bytes")
        spoken_phrases = [call.args[0] for call in cog._tts.synthesize.await_args_list]
        assert any("Поиск треков сейчас недоступен." in phrase for phrase in spoken_phrases)


class TestModelNotReadySkipsVoiceButKeepsText:
    """Требование 2: неготовая модель не мешает вернуть текст, но и не озвучивает ошибку."""

    async def test_text_returned_without_speaking(self) -> None:
        """При неготовой модели ошибка всё равно возвращается текстом."""
        cog = _make_cog(tts_ready=False)
        cog._dispatch_voice_command = AsyncMock(
            side_effect=SearchUnavailableError(user_message="Поиск треков сейчас недоступен.")
        )

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Поиск треков сейчас недоступен."
        cog._tts.synthesize.assert_not_called()
        cog._player.say.assert_not_called()


class TestVoiceRepliesDisabledSkipsVoiceButKeepsText:
    """Требование 3: выключенные голосовые реплики не озвучивают ошибку, текст остаётся."""

    async def test_text_returned_without_speaking(self) -> None:
        """При `voice_replies = False` ошибка не произносится голосом, текст возвращается."""
        cog = _make_cog(tts_ready=True, voice_replies=False)
        cog._dispatch_voice_command = AsyncMock(
            side_effect=SearchUnavailableError(user_message="Поиск треков сейчас недоступен.")
        )

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Поиск треков сейчас недоступен."
        cog._tts.synthesize.assert_not_called()
        cog._player.say.assert_not_called()


class TestSynthesisErrorDuringErrorSpeechDoesNotBreakCommand:
    """Требование 4 (главная защита от рекурсии): сбой синтеза при озвучивании ошибки не мешает."""

    async def test_command_result_unaffected_by_synthesis_error(self) -> None:
        """Ошибка `synthesize` при попытке озвучить ошибку не мешает вернуть текст-подтверждение."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = AsyncMock(
            side_effect=SearchUnavailableError(user_message="Поиск треков сейчас недоступен.")
        )
        cog._tts.synthesize = AsyncMock(
            side_effect=SpeechSynthesisUnavailableError(
                "синтез упал", user_message="не вышло сказать"
            )
        )

        result = await cog.execute_voice_command(VoiceCommand(action="pause"), speaker=None)

        assert result == "Поиск треков сейчас недоступен."
        cog._player.say.assert_not_called()


class TestStopPathErrorSpeech:
    """Те же требования для `stop` — единственного последовательного пути голосовых команд."""

    async def test_speaks_phrase_containing_user_message_and_returns_it(self) -> None:
        """`stop`: бот произносит фразу с причиной ошибки и возвращает тот же текст в чат."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = AsyncMock(
            side_effect=BotError(user_message="Бот не подключён к голосовому каналу.")
        )

        result = await cog.execute_voice_command(VoiceCommand(action="stop"), speaker=None)

        assert result == "Бот не подключён к голосовому каналу."
        spoken_phrases = [call.args[0] for call in cog._tts.synthesize.await_args_list]
        assert any(
            "Бот не подключён к голосовому каналу." in phrase for phrase in spoken_phrases
        )

    async def test_synthesis_error_does_not_break_command(self) -> None:
        """`stop`: сбой синтеза при озвучивании ошибки не мешает вернуть текст-подтверждение."""
        cog = _make_cog(tts_ready=True)
        cog._dispatch_voice_command = AsyncMock(
            side_effect=BotError(user_message="Бот не подключён к голосовому каналу.")
        )
        cog._tts.synthesize = AsyncMock(
            side_effect=SpeechSynthesisUnavailableError(
                "синтез упал", user_message="не вышло сказать"
            )
        )

        result = await cog.execute_voice_command(VoiceCommand(action="stop"), speaker=None)

        assert result == "Бот не подключён к голосовому каналу."


class TestEmptySearchResultSpokenAloud:
    """Дополнение: пустой результат поиска — не `BotError`, но тоже должен звучать голосом."""

    async def test_speaks_phrase_and_returns_same_text(self) -> None:
        """Бот произносит фразу о том, что ничего не найдено, и возвращает тот же текст."""
        cog = _make_cog(tts_ready=True)
        cog._resolve_search_track = AsyncMock(return_value=None)

        result = await cog._search_and_start_wave_voice("группа кино", speaker=None)

        # Текст в чат остаётся полным ("Ничего не найдено по запросу…"), а
        # произносится вслух только сам запрос — вводная фраза уже содержит
        # "ничего", и повтор из чата тавтологичен (см. докстринг
        # `_search_and_start_wave_voice`).
        assert result == "Ничего не найдено по запросу «группа кино»."
        cog._player.say.assert_awaited_with(b"pcm-bytes")
        spoken_phrases = [call.args[0] for call in cog._tts.synthesize.await_args_list]
        assert any("группа кино" in phrase for phrase in spoken_phrases)

    async def test_voice_replies_disabled_returns_text_only(self) -> None:
        """При выключенных репликах пустой результат поиска не озвучивается, только текст."""
        cog = _make_cog(tts_ready=True, voice_replies=False)
        cog._resolve_search_track = AsyncMock(return_value=None)

        result = await cog._search_and_start_wave_voice("группа кино", speaker=None)

        assert result == "Ничего не найдено по запросу «группа кино»."
        cog._tts.synthesize.assert_not_called()
        cog._player.say.assert_not_called()
