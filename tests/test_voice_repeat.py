"""Тесты голосовой команды «повтори»: бот произносит сказанное вслух."""

from __future__ import annotations

import pytest

from bot.voice_commands import VoiceCommand, describe_command, parse_voice_command


class TestRepeatParsing:
    """Разбор просьбы повторить фразу."""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("катя повтори привет всем", "привет всем"),
            ("катя повтори за мной я робот", "я робот"),
            ("катя повтори за мною хорошего вечера", "хорошего вечера"),
            ("катя повтори вслух это тест", "это тест"),
            ("катя скажи всем привет", "всем привет"),
            ("катя произнеси добрый вечер", "добрый вечер"),
            ("гурген повтори раз два три", "раз два три"),
        ],
    )
    def test_text_is_extracted(self, text, expected):
        """Произносимым становится всё, что сказано после командного слова."""
        command = parse_voice_command(text)
        assert command is not None
        assert command.action == "repeat"
        assert command.query == expected

    def test_service_words_are_kept(self):
        """Служебные слова внутри фразы сохраняются — человек просил повторить именно их.

        Этим «повтори» отличается от поиска: там «пожалуйста» и «трек» —
        шум вокруг запроса, а здесь всё, что после команды, и есть текст.
        """
        command = parse_voice_command("катя повтори слово пожалуйста")
        assert command is not None
        assert command.query == "слово пожалуйста"

    @pytest.mark.parametrize("text", ["катя повтори", "катя скажи", "катя произнеси"])
    def test_without_text_is_not_a_command(self, text):
        """Голое «повтори» командой не считается: произносить нечего."""
        assert parse_voice_command(text) is None

    def test_requires_wake_word(self):
        """Без обращения по имени просьба повторить не выполняется."""
        assert parse_voice_command("повтори привет") is None

    def test_past_tense_is_not_a_command(self):
        """Пересказ «повторила за мной» не должен запускать произнесение."""
        assert parse_voice_command("катя повторила за мной") is None
        assert parse_voice_command("я кате сказал привет") is None


class TestRepeatVersusNowPlaying:
    """Разграничение «скажи …» и вопроса о текущем треке."""

    @pytest.mark.parametrize(
        "text",
        ["катя скажи что играет", "катя скажи кто поет", "катя скажи что за песня"],
    )
    def test_question_wins_over_repeat(self, text):
        """«Скажи, что играет» — вопрос о треке, а не просьба произнести эти слова.

        Порядок правил в `_RULES` держит `now_playing` выше `repeat`
        именно ради этого; перестановка сломает разграничение.
        """
        command = parse_voice_command(text)
        assert command is not None
        assert command.action == "now_playing"

    def test_plain_repeat_still_works(self):
        """Обычная просьба повторить вопросом не перехватывается."""
        command = parse_voice_command("катя повтори доброе утро")
        assert command is not None
        assert command.action == "repeat"


class TestOtherCommandsIntact:
    """Появление «повтори» не должно ломать остальные команды."""

    @pytest.mark.parametrize(
        ("text", "action"),
        [
            ("катя следующий трек", "skip"),
            ("катя пауза", "pause"),
            ("катя включи мою волну", "wave"),
            ("катя включи кино группа крови", "search"),
            ("катя громкость сорок", "volume"),
            ("катя отключись", "stop"),
        ],
    )
    def test_existing_commands(self, text, action):
        """Прежние команды разбираются как раньше."""
        command = parse_voice_command(text)
        assert command is not None
        assert command.action == action


class TestDescribeRepeat:
    """Короткое описание для мгновенного подтверждения в чате."""

    def test_description_contains_text(self):
        """В описании видно, что именно бот собирается произнести."""
        described = describe_command(VoiceCommand(action="repeat", query="привет всем"))
        assert "привет всем" in described

    def test_description_without_text(self):
        """Описание без текста не падает и не остаётся пустым."""
        assert describe_command(VoiceCommand(action="repeat"))
