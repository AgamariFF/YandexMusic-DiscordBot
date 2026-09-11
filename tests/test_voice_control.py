"""Тесты кога голосового управления — прежде всего окна ожидания команды после оклика.

Ког создаётся с поддельными `bot`/`Config`: ни `SpeechRecognizer`, ни
`GuildListener` в конструкторе ничего не грузят и никуда не подключаются
(модель читается только в `ensure_ready`, соединение — только в `attach`),
поэтому для проверки разбора фраз ни модель Vosk, ни Discord не нужны.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from bot.cogs.voice_control import WAKE_FOLLOW_UP_SECONDS, VoiceControlCog
from bot.config import Config

SPEAKER = 111
OTHER_SPEAKER = 222


def _make_config() -> Config:
    """Собирает минимальный конфиг — для разбора фраз важен только сам объект."""
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
    )


@pytest.fixture
def cog() -> VoiceControlCog:
    """Ког голосового управления без реальных модели и голосового соединения."""
    return VoiceControlCog(MagicMock(), _make_config())


class TestSinglePhraseCommand:
    """Команда, целиком уместившаяся в одну распознанную фразу."""

    def test_full_phrase_is_parsed(self, cog: VoiceControlCog) -> None:
        """«катя следующий трек» одной фразой разбирается сразу."""
        command = cog._parse_phrase(SPEAKER, "катя следующий трек")
        assert command is not None
        assert command.action == "skip"

    def test_phrase_without_name_is_ignored(self, cog: VoiceControlCog) -> None:
        """Фраза без обращения по имени командой не считается."""
        assert cog._parse_phrase(SPEAKER, "следующее") is None

    def test_short_skip_without_track_word(self, cog: VoiceControlCog) -> None:
        """«катя следующий» без слова «трек» — тоже пропуск трека."""
        command = cog._parse_phrase(SPEAKER, "катя следующий")
        assert command is not None
        assert command.action == "skip"


class TestWakeWindow:
    """Оклик по имени отдельной фразой и команда следующей фразой."""

    def test_bare_name_is_not_a_command(self, cog: VoiceControlCog) -> None:
        """Один только оклик по имени командой не является."""
        assert cog._parse_phrase(SPEAKER, "катя") is None

    def test_command_after_bare_name(self, cog: VoiceControlCog) -> None:
        """Главный сценарий: «катя» и «следующее» приехали двумя фразами подряд."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        command = cog._parse_phrase(SPEAKER, "следующее")
        assert command is not None
        assert command.action == "skip"

    def test_command_after_bare_name_with_query(self, cog: VoiceControlCog) -> None:
        """Поисковая команда второй фразой сохраняет запрос."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        command = cog._parse_phrase(SPEAKER, "включи кино группа крови")
        assert command is not None
        assert command.action == "search"
        assert command.query == "кино группа крови"

    def test_window_is_single_use(self, cog: VoiceControlCog) -> None:
        """Окно закрывается первой же следующей фразой — вторая уже не команда."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        assert cog._parse_phrase(SPEAKER, "следующее") is not None
        assert cog._parse_phrase(SPEAKER, "поставь на паузу") is None

    def test_window_closes_even_on_unrecognized_phrase(self, cog: VoiceControlCog) -> None:
        """Нераспознанная фраза тоже закрывает окно, а не оставляет его открытым."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        assert cog._parse_phrase(SPEAKER, "да я не тебе говорю") is None
        assert cog._parse_phrase(SPEAKER, "следующее") is None

    def test_window_belongs_to_one_speaker(self, cog: VoiceControlCog) -> None:
        """Окно открыто только для окликнувшего — чужая реплика командой не станет."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        assert cog._parse_phrase(OTHER_SPEAKER, "следующее") is None

    def test_window_expires(self, cog: VoiceControlCog, monkeypatch: pytest.MonkeyPatch) -> None:
        """По истечении окна следующая фраза уже не принимается за команду."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        later = cog._awake_until + 1.0
        monkeypatch.setattr("bot.cogs.voice_control.time.monotonic", lambda: later)
        assert cog._parse_phrase(SPEAKER, "следующее") is None

    def test_window_still_open_just_before_expiry(
        self, cog: VoiceControlCog, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Прямо перед истечением окна команда ещё принимается."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        almost = cog._awake_until - 0.5
        monkeypatch.setattr("bot.cogs.voice_control.time.monotonic", lambda: almost)
        command = cog._parse_phrase(SPEAKER, "следующее")
        assert command is not None
        assert command.action == "skip"

    def test_window_length_matches_constant(self, cog: VoiceControlCog) -> None:
        """Окно открывается ровно на `WAKE_FOLLOW_UP_SECONDS` от момента оклика."""
        import time

        before = time.monotonic()
        cog._parse_phrase(SPEAKER, "катя")
        assert cog._awake_until >= before + WAKE_FOLLOW_UP_SECONDS

    def test_full_command_closes_stale_window(self, cog: VoiceControlCog) -> None:
        """Полная команда с именем закрывает ранее открытое окно."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        assert cog._parse_phrase(SPEAKER, "катя поставь на паузу") is not None
        assert cog._awake_speaker is None
        assert cog._parse_phrase(SPEAKER, "следующее") is None

    def test_repeated_name_restarts_window(self, cog: VoiceControlCog) -> None:
        """Повторный оклик заново открывает окно, а не закрывает его."""
        assert cog._parse_phrase(SPEAKER, "катя") is None
        assert cog._parse_phrase(SPEAKER, "кате") is None
        command = cog._parse_phrase(SPEAKER, "следующее")
        assert command is not None
        assert command.action == "skip"

    def test_filler_before_name_still_opens_window(self, cog: VoiceControlCog) -> None:
        """Оклик со словом-заполнителем («эй катя») тоже открывает окно."""
        assert cog._parse_phrase(SPEAKER, "эй катя") is None
        command = cog._parse_phrase(SPEAKER, "следующее")
        assert command is not None
        assert command.action == "skip"

    def test_no_window_without_name(self, cog: VoiceControlCog) -> None:
        """Без оклика по имени команда без имени не выполняется."""
        assert cog._parse_phrase(SPEAKER, "следующее") is None
        assert cog._parse_phrase(SPEAKER, "поставь на паузу") is None
