"""Тесты озвучивания в плеере: фраза вклинивается в музыку и возвращает её обратно."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from bot.audio.speaking import FRAME_BYTES, SpeakingSource
from bot.errors import NotConnectedError
from bot.player import GuildPlayer

SPEECH = b"\x22" * (FRAME_BYTES * 2)


def _make_player() -> GuildPlayer:
    """Плеер с поддельным клиентом Яндекса — до сети дело здесь не доходит."""
    return GuildPlayer(MagicMock())


def _make_voice_client(*, playing: bool = True, paused: bool = False) -> MagicMock:
    """Поддельное голосовое соединение с управляемыми признаками состояния."""
    voice_client = MagicMock()
    voice_client.is_playing.return_value = playing
    voice_client.is_paused.return_value = paused
    return voice_client


class TestSayWithoutConnection:
    """Озвучивание без голосового соединения."""

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self):
        """Без соединения озвучивание сообщает понятную доменную ошибку."""
        with pytest.raises(NotConnectedError):
            await _make_player().say(SPEECH)

    @pytest.mark.asyncio
    async def test_empty_speech_is_noop(self):
        """Пустая речь ничего не делает и не требует соединения."""
        await _make_player().say(b"")


class TestSayOverMusic:
    """Фраза поверх играющего трека."""

    @pytest.mark.asyncio
    async def test_speech_goes_into_existing_source(self):
        """Фраза отдаётся уже играющему источнику, а не запускает второй.

        Discord играет в соединении ровно один источник: второй `play()`
        поверх первого потерял бы музыку.
        """
        player = _make_player()
        voice_client = _make_voice_client()
        source = SpeakingSource(None, loop=asyncio.get_running_loop())
        player._voice_client = voice_client
        player._source = source

        task = asyncio.create_task(player.say(SPEECH))
        await asyncio.sleep(0)
        assert source.is_speaking
        voice_client.play.assert_not_called()

        while source.is_speaking:
            source.read()
        await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_waits_until_phrase_is_finished(self):
        """Вызов ждёт, пока фраза договорена, а не возвращается сразу."""
        player = _make_player()
        player._voice_client = _make_voice_client()
        source = SpeakingSource(None, loop=asyncio.get_running_loop())
        player._source = source

        task = asyncio.create_task(player.say(SPEECH))
        await asyncio.sleep(0)
        assert not task.done()

        while source.is_speaking:
            source.read()
        await asyncio.wait_for(task, timeout=1.0)
        assert task.done()


class TestSayWithoutMusic:
    """Фраза, когда ничего не играет."""

    @pytest.mark.asyncio
    async def test_plays_standalone_source(self):
        """Без играющего трека фраза запускается отдельным источником."""
        player = _make_player()
        voice_client = _make_voice_client(playing=False)
        player._voice_client = voice_client
        player._source = None

        task = asyncio.create_task(player.say(SPEECH))
        await asyncio.sleep(0)

        voice_client.play.assert_called_once()
        source = voice_client.play.call_args.args[0]
        assert isinstance(source, SpeakingSource)
        assert source.music is None

        while source.is_speaking:
            source.read()
        await asyncio.wait_for(task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_standalone_source_is_not_kept(self):
        """Источник ради одной фразы не подменяет собой музыкальный.

        Иначе после фразы плеер считал бы, что играет трек, и обновление
        сообщения-плеера показывало бы несуществующее воспроизведение.
        """
        player = _make_player()
        voice_client = _make_voice_client(playing=False)
        player._voice_client = voice_client

        task = asyncio.create_task(player.say(SPEECH))
        await asyncio.sleep(0)
        source = voice_client.play.call_args.args[0]
        while source.is_speaking:
            source.read()
        await asyncio.wait_for(task, timeout=1.0)

        assert player._source is None


class TestSayWhilePaused:
    """Фраза, когда музыка стоит на паузе."""

    @pytest.mark.asyncio
    async def test_resumes_and_pauses_back(self):
        """Пауза снимается на время фразы и возвращается после.

        Приостановленное соединение не читает источник вовсе, поэтому без
        снятия паузы фразу не было бы слышно.
        """
        player = _make_player()
        voice_client = _make_voice_client(playing=True, paused=True)
        player._voice_client = voice_client
        source = SpeakingSource(None, loop=asyncio.get_running_loop())
        player._source = source

        task = asyncio.create_task(player.say(SPEECH))
        await asyncio.sleep(0)
        voice_client.resume.assert_called_once()

        while source.is_speaking:
            source.read()
        await asyncio.wait_for(task, timeout=1.0)
        voice_client.pause.assert_called_once()

    @pytest.mark.asyncio
    async def test_playing_music_is_not_paused_afterwards(self):
        """Игравшая музыка после фразы не оказывается на паузе."""
        player = _make_player()
        voice_client = _make_voice_client(playing=True, paused=False)
        player._voice_client = voice_client
        source = SpeakingSource(None, loop=asyncio.get_running_loop())
        player._source = source

        task = asyncio.create_task(player.say(SPEECH))
        await asyncio.sleep(0)
        while source.is_speaking:
            source.read()
        await asyncio.wait_for(task, timeout=1.0)

        voice_client.pause.assert_not_called()
        voice_client.resume.assert_not_called()
