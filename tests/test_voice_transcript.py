"""Тесты приватности постоянного распознавания: публикация речи только за флагом SPEECH_TRANSCRIPT.

Баг, из-за которого бот дублировал в текстовый канал ВСЁ услышанное, был не
в коде, а в `.env` пользователя (там стоял отладочный `SPEECH_TRANSCRIPT=1`),
и уже устранён отдельно. Эти тесты закрепляют само поведение по умолчанию —
`bot.cogs.voice_control.VoiceControlCog._process_phrase` не должен публиковать
распознанную речь, если флаг явно не включён, — и разбор самого флага в
`bot.config._parse_bool_env`, чтобы ни то ни другое нельзя было сломать
незаметно.

Ког создаётся с поддельными `bot`/`Config` (см. `tests/test_voice_control.py`
— тот же приём): ни `SpeechRecognizer`, ни `GuildListener` в конструкторе
ничего не грузят и никуда не подключаются. `MusicCog` заменён на `AsyncMock`
— он нужен только как источник текстового канала (`announce_channel`) и
исполнитель команды (`execute_voice_command`), реальная музыкальная логика
здесь не участвует. Discord-канал — тоже `AsyncMock`: у `AsyncMock` дочерние
атрибуты сами оказываются `AsyncMock`, поэтому `channel.send(...)` и
`ack.edit(...)`/`ack.delete(...)` на возвращённом «сообщении» уже awaitable
без ручной настройки каждого метода по отдельности.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.cogs.voice_control import VoiceControlCog
from bot.config import Config, _parse_bool_env

SPEAKER = 111

# Фраза без обращения по имени — заведомо не команда боту.
NOT_A_COMMAND_PHRASE = "давайте закажем пиццу после игры"

# Команда без имени в наборе «медленных» (`_SLOW_ACTIONS`) — подтверждение
# ДО выполнения не шлётся, есть только итоговое сообщение через `_finish_ack`.
FAST_COMMAND_PHRASE = "катя поставь на паузу"

# Команда из набора «медленных» — подтверждение шлётся отдельным сообщением
# (`_send_ack`), а затем редактируется в итог (`_finish_ack`).
SLOW_COMMAND_PHRASE = "катя следующий трек"


def _make_config(*, speech_transcript: bool) -> Config:
    """Собирает минимальный конфиг — важен только сам объект и флаг транскрипта."""
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
        speech_transcript=speech_transcript,
        speech_debug_dir="",
        speech_debug_max_seconds=600.0,
        tts_enabled=True,
        tts_model_name="vosk-model-tts-ru-0.7-multi",
        tts_speaker_id=2,
    )


def _make_cog(*, speech_transcript: bool) -> tuple[VoiceControlCog, AsyncMock]:
    """Собирает ког с привязанным поддельным `MusicCog` и текстовым каналом.

    Возвращает сам ког и мок канала — тесты проверяют по нему, что и сколько
    раз ушло в Discord через `channel.send`.
    """
    cog = VoiceControlCog(MagicMock(), _make_config(speech_transcript=speech_transcript))
    channel = AsyncMock()
    music_cog = AsyncMock()
    music_cog.announce_channel = channel
    music_cog.execute_voice_command.return_value = "готово"
    cog.bind_music_cog(music_cog)
    return cog, channel


class TestTranscriptDisabledByDefault:
    """При выключенном (по умолчанию) флаге распознанная речь в канал не уходит."""

    async def test_non_command_phrase_sends_nothing(self) -> None:
        """Фраза без обращения к боту не порождает ни одного `channel.send`."""
        cog, channel = _make_cog(speech_transcript=False)
        await cog._process_phrase(SPEAKER, NOT_A_COMMAND_PHRASE)
        channel.send.assert_not_called()

    async def test_fast_command_sends_only_result_without_raw_phrase(self) -> None:
        """Мгновенная команда шлёт один итог, а не саму распознанную фразу."""
        cog, channel = _make_cog(speech_transcript=False)
        await cog._process_phrase(SPEAKER, FAST_COMMAND_PHRASE)
        assert channel.send.call_count == 1
        sent_text = channel.send.call_args.args[0]
        assert sent_text == "готово"
        assert FAST_COMMAND_PHRASE not in sent_text

    async def test_slow_command_ack_does_not_contain_raw_phrase(self) -> None:
        """У «медленной» команды подтверждение — описание команды, а не сама фраза целиком."""
        cog, channel = _make_cog(speech_transcript=False)
        await cog._process_phrase(SPEAKER, SLOW_COMMAND_PHRASE)
        assert channel.send.call_count == 1
        ack_text = channel.send.call_args.args[0]
        assert SLOW_COMMAND_PHRASE not in ack_text
        ack_message = channel.send.return_value
        ack_message.edit.assert_awaited_once()
        assert SLOW_COMMAND_PHRASE not in ack_message.edit.call_args.kwargs["content"]


class TestTranscriptEnabledExplicitly:
    """Включённый флаг — рабочая отладочная возможность, публикация не должна пропасть."""

    async def test_non_command_phrase_is_published(self) -> None:
        """При явно включённом флаге даже не-команда публикуется целиком."""
        cog, channel = _make_cog(speech_transcript=True)
        await cog._process_phrase(SPEAKER, NOT_A_COMMAND_PHRASE)
        channel.send.assert_called_once()
        sent_text = channel.send.call_args.args[0]
        assert NOT_A_COMMAND_PHRASE in sent_text

    async def test_command_phrase_is_published_in_addition_to_result(self) -> None:
        """При включённом флаге команда всё равно и публикуется целиком, и выполняется."""
        cog, channel = _make_cog(speech_transcript=True)
        await cog._process_phrase(SPEAKER, FAST_COMMAND_PHRASE)
        assert channel.send.call_count == 2
        transcript_text = channel.send.call_args_list[0].args[0]
        result_text = channel.send.call_args_list[1].args[0]
        assert FAST_COMMAND_PHRASE in transcript_text
        assert result_text == "готово"


class TestParseBoolEnv:
    """Разбор `SPEECH_TRANSCRIPT` (и любой другой булевой переменной) через `_parse_bool_env`."""

    @pytest.mark.parametrize(
        "raw",
        ["0", "false", "FALSE", "False", "no", "NO", "off", "OFF", "мусор", "нет", " "],
    )
    def test_falsy_values_are_false(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """Явно ложные значения и произвольный мусор разбираются как `False`."""
        monkeypatch.setenv("SPEECH_TRANSCRIPT", raw)
        assert _parse_bool_env("SPEECH_TRANSCRIPT", default=False) is False

    def test_missing_variable_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Переменной нет вовсе — берётся `default`, а не что-то другое."""
        monkeypatch.delenv("SPEECH_TRANSCRIPT", raising=False)
        assert _parse_bool_env("SPEECH_TRANSCRIPT", default=False) is False

    def test_empty_variable_uses_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Переменная задана пустой строкой — тоже берётся `default`."""
        monkeypatch.setenv("SPEECH_TRANSCRIPT", "")
        assert _parse_bool_env("SPEECH_TRANSCRIPT", default=False) is False

    @pytest.mark.parametrize(
        "raw", ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " 1 ", " True "]
    )
    def test_truthy_values_are_true(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """Только `1`/`true`/`yes`/`on` (без учёта регистра и краевых пробелов) — истина."""
        monkeypatch.setenv("SPEECH_TRANSCRIPT", raw)
        assert _parse_bool_env("SPEECH_TRANSCRIPT", default=False) is True

    def test_default_is_honored_when_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`default` — это именно параметр функции, а не жёстко зашитое `False`."""
        monkeypatch.delenv("SPEECH_TRANSCRIPT", raising=False)
        assert _parse_bool_env("SPEECH_TRANSCRIPT", default=True) is True
