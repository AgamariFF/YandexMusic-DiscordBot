"""Источник звука, умеющий вклиниться в музыку голосом бота и вернуть музыку обратно.

Discord отдаёт на сервер одно голосовое соединение и играет в нём ровно
один источник: попытка запустить второй поверх первого либо отвергается,
либо теряет первый. Поэтому «сказать фразу поверх музыки» решается не
вторым воспроизведением, а подменой на уровне источника — `SpeakingSource`
оборачивает музыкальный источник и, пока есть что произнести, отдаёт
Discord кадры речи вместо музыкальных.

Музыка при этом именно ПРИОСТАНАВЛИВАЕТСЯ, а не играет фоном: пока звучит
фраза, из музыкального источника не читается ни одного кадра, а
`TrackedAudioSource.elapsed` считает только прочитанные кадры — значит
позиция трека стоит, и после фразы он продолжится ровно с того места, где
его прервали. Это и требовалось: голос бота не должен тонуть в музыке.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from bot.audio.source import TrackedAudioSource
from bot.nekto.audio import (
    DISCORD_CHANNELS,
    DISCORD_FRAME_DURATION_MS,
    DISCORD_SAMPLE_RATE,
    DISCORD_SAMPLE_WIDTH,
)

logger = logging.getLogger(__name__)

# Размер кадра, которого Discord ждёт от источника: ровно 20 мс звука.
FRAME_BYTES = (
    DISCORD_SAMPLE_RATE
    * DISCORD_FRAME_DURATION_MS
    // 1000
    * DISCORD_CHANNELS
    * DISCORD_SAMPLE_WIDTH
)

SILENCE_FRAME = b"\x00" * FRAME_BYTES


class SpeakingSource(discord.AudioSource):
    """Отдаёт Discord речь бота, когда она есть, и музыку — когда речи нет.

    Оборачивает музыкальный источник (может быть и `None` — тогда источник
    существует только ради одной фразы и заканчивается вместе с ней).
    `read()` дёргается Discord из отдельного потока каждые 20 мс, поэтому
    буфер речи меняется под локом, а событие окончания фразы выставляется
    через `call_soon_threadsafe` — трогать `asyncio.Event` напрямую из
    чужого потока нельзя.
    """

    def __init__(
        self,
        music: TrackedAudioSource | None,
        *,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Запоминает музыкальный источник и цикл событий, которому принадлежит владелец."""
        self._music = music
        self._loop = loop
        self._speech = b""
        self._offset = 0
        self._finished: asyncio.Event | None = None

    @property
    def music(self) -> TrackedAudioSource | None:
        """Обёрнутый музыкальный источник, если он есть."""
        return self._music

    @property
    def is_speaking(self) -> bool:
        """Признак того, что прямо сейчас произносится фраза."""
        return self._offset < len(self._speech)

    @property
    def volume(self) -> float:
        """Громкость музыки — делегируется обёрнутому источнику."""
        return self._music.volume if self._music is not None else 1.0

    @volume.setter
    def volume(self, value: float) -> None:
        """Меняет громкость музыки; на громкость речи не влияет намеренно.

        Речь бота — это ответ человеку, а не часть фонограммы: приглушать
        её вместе с музыкой значило бы сделать ответ неслышным именно
        тогда, когда музыку сделали тише.
        """
        if self._music is not None:
            self._music.volume = value

    @property
    def elapsed(self) -> float:
        """Позиция в треке — делегируется обёрнутому источнику."""
        return self._music.elapsed if self._music is not None else 0.0

    def speak(self, pcm: bytes) -> asyncio.Event:
        """Ставит фразу на произнесение и возвращает событие её окончания.

        Новая фраза заменяет недоговорённую предыдущую, а не встаёт за ней
        в очередь: команда «повтори» отдаётся голосом, и если человек
        передумал и сказал другую фразу, ждать окончания первой ему незачем.
        Событие прошлой фразы при этом выставляется — иначе тот, кто её ждал,
        завис бы навсегда.
        """
        previous = self._finished
        finished = asyncio.Event()
        self._speech = pcm
        self._offset = 0
        self._finished = finished
        if previous is not None and not previous.is_set():
            previous.set()
        if not pcm:
            finished.set()
            self._finished = None
        return finished

    def read(self) -> bytes:
        """Отдаёт очередные 20 мс: речь, если она есть, иначе музыку.

        Вызывается Discord из собственного потока воспроизведения, а не из
        потока событий.
        """
        if self._offset < len(self._speech):
            chunk = self._speech[self._offset : self._offset + FRAME_BYTES]
            self._offset += FRAME_BYTES
            if self._offset >= len(self._speech):
                self._finish_speech()
            if len(chunk) < FRAME_BYTES:
                # Последний кадр фразы почти всегда неполный: Discord ждёт
                # ровно 20 мс, поэтому добиваем тишиной, а не отдаём обрезок
                # — обрезок он посчитал бы концом воспроизведения.
                chunk += SILENCE_FRAME[: FRAME_BYTES - len(chunk)]
            return chunk

        if self._music is None:
            # Источник существовал только ради фразы — она закончилась,
            # и воспроизводить больше нечего.
            return b""
        return self._music.read()

    def _finish_speech(self) -> None:
        """Сообщает ожидающему, что фраза договорена. Вызывается из потока Discord."""
        finished = self._finished
        self._finished = None
        if finished is None:
            return
        try:
            self._loop.call_soon_threadsafe(finished.set)
        except RuntimeError:
            # Цикл событий уже закрыт (бот останавливается) — ждать
            # окончания фразы всё равно некому.
            logger.debug("Цикл событий закрыт, некому сообщить об окончании фразы")

    def is_opus(self) -> bool:
        """Источник отдаёт сырой PCM, а не Opus — как и обёрнутый музыкальный."""
        return False

    def cleanup(self) -> None:
        """Освобождает музыкальный источник и снимает ожидание недоговорённой фразы."""
        self._finish_speech()
        if self._music is not None:
            self._music.cleanup()
