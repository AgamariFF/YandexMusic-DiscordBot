"""Тесты фонового прогрева модели распознавания при старте кога.

Без прогрева модель грузится лениво — при первом подключении к голосовому
каналу, внутри хука, который `GuildPlayer.connect()` дожидается. С большой
моделью (`vosk-model-ru-0.42`, около 146 с на загрузку) это означало бы, что
первый `/wave` после запуска бота висит две с половиной минуты, а окно
ответа Discord на interaction протухает задолго до конца.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.cogs.voice_control import VoiceControlCog
from bot.config import Config
from bot.errors import SpeechModelUnavailableError


def _make_config() -> Config:
    """Минимальный конфиг: для прогрева важен только сам объект."""
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
        speech_model_path="models/vosk-model-ru-0.42",
        speech_enabled=True,
        speech_transcript=False,
        speech_debug_dir="",
        speech_debug_max_seconds=600.0,
        tts_enabled=True,
        tts_model_name="vosk-model-tts-ru-0.7-multi",
        tts_speaker_id=2,
        voice_replies=True,
    )


@pytest.fixture
def cog() -> VoiceControlCog:
    """Ког с подменённым распознавателем — настоящая модель не грузится."""
    cog = VoiceControlCog(MagicMock(), _make_config())
    cog._recognizer = MagicMock()
    cog._recognizer.ensure_ready = AsyncMock()
    return cog


class TestWarmUpScheduling:
    """Контракт `cog_load`: прогрев запускается фоном, а не блокирует загрузку кога."""

    @pytest.mark.asyncio
    async def test_cog_load_warms_the_model(self, cog: VoiceControlCog) -> None:
        """Модель начинает грузиться сразу при старте, до первого подключения к каналу."""
        await cog.cog_load()
        await cog._model_warmup_task

        cog._recognizer.ensure_ready.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cog_load_does_not_block(self, cog: VoiceControlCog) -> None:
        """`cog_load` возвращается, не дожидаясь загрузки — иначе встал бы запуск бота."""
        release = asyncio.Event()

        async def slow_load() -> None:
            await release.wait()

        cog._recognizer.ensure_ready = AsyncMock(side_effect=slow_load)

        await asyncio.wait_for(cog.cog_load(), timeout=1)

        assert cog._model_warmup_task is not None
        assert not cog._model_warmup_task.done()

        release.set()
        await cog._model_warmup_task

    @pytest.mark.asyncio
    async def test_disabled_speech_skips_warmup(self, cog: VoiceControlCog) -> None:
        """Уже отключённое распознавание не грузит модель впустую."""
        cog._speech_disabled = True

        await cog.cog_load()

        assert cog._model_warmup_task is None
        cog._recognizer.ensure_ready.assert_not_awaited()


class TestWarmUpOutcome:
    """Контракт `_warm_up_model`: чем заканчивается прогрев и что попадает в лог."""

    @pytest.mark.asyncio
    async def test_success_is_logged_with_duration(
        self, cog: VoiceControlCog, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Успешная готовность отмечается в логе — по ней видно цену запуска."""
        with caplog.at_level(logging.INFO, logger="bot.cogs.voice_control"):
            await cog._warm_up_model()

        assert any("готова за" in record.getMessage() for record in caplog.records)
        assert cog._speech_disabled is False

    @pytest.mark.asyncio
    async def test_missing_model_disables_speech(
        self, cog: VoiceControlCog, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Нет модели на диске — голосовое управление выключается, музыка продолжает работать."""
        cog._recognizer.ensure_ready = AsyncMock(
            side_effect=SpeechModelUnavailableError(
                "нет модели", user_message="Модель распознавания речи не найдена на диске."
            )
        )

        with caplog.at_level(logging.WARNING, logger="bot.cogs.voice_control"):
            await cog._warm_up_model()

        assert cog._speech_disabled is True
        assert cog._model_unavailable_logged is True
        assert len(caplog.records) == 1

    @pytest.mark.asyncio
    async def test_missing_model_is_logged_once(
        self, cog: VoiceControlCog, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Повторная неудача не засоряет лог — сообщение одно на весь процесс."""
        cog._recognizer.ensure_ready = AsyncMock(
            side_effect=SpeechModelUnavailableError("нет модели", user_message="Нет модели.")
        )

        with caplog.at_level(logging.WARNING, logger="bot.cogs.voice_control"):
            await cog._warm_up_model()
            await cog._warm_up_model()

        assert len(caplog.records) == 1

    @pytest.mark.asyncio
    async def test_unexpected_error_does_not_escape(
        self, cog: VoiceControlCog, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Непредвиденный сбой прогрева не роняет запуск бота, а уходит в лог."""
        cog._recognizer.ensure_ready = AsyncMock(side_effect=RuntimeError("bang"))

        with caplog.at_level(logging.ERROR, logger="bot.cogs.voice_control"):
            await cog._warm_up_model()

        assert any(record.exc_info for record in caplog.records)

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, cog: VoiceControlCog) -> None:
        """Отмена остаётся отменой: иначе `cog_unload` не смог бы дождаться задачи."""
        cog._recognizer.ensure_ready = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await cog._warm_up_model()


class TestWarmUpShutdown:
    """Контракт `cog_unload`: незавершённый прогрев не переживает выгрузку кога."""

    @pytest.mark.asyncio
    async def test_unload_cancels_pending_warmup(self, cog: VoiceControlCog) -> None:
        """Выгрузка не ждёт окончания загрузки модели, а отменяет её."""
        never = asyncio.Event()
        cog._recognizer.ensure_ready = AsyncMock(side_effect=never.wait)
        cog._recognizer.close = AsyncMock()
        cog._listener.detach = AsyncMock()

        await cog.cog_load()
        task = cog._model_warmup_task
        assert task is not None

        await cog.cog_unload()

        assert task.cancelled()
