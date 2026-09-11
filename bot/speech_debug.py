"""Отладочная запись всего, что бот слышит: WAV, расшифровка и статистика потерь пакетов.

Отвечает ровно на один вопрос: **виновата модель распознавания или доставка
звука?** Разделить их на слух по итоговому тексту невозможно — неверный
текст выглядит одинаково и когда модель не разобрала чистую речь, и когда
до модели доехала речь с дырами. Поэтому модуль сохраняет три вещи рядом:

* `speaker-<id>.wav` — звук ровно в том виде, в каком он уходит в
  распознавание (16 кГц моно, уже после ресемплинга). Это и есть ответ:
  если запись звучит чисто, а текст неверный — дело в модели; если запись
  рваная, речь «съедена» или ускорена — дело в доставке, и модель тут ни
  при чём;
* `transcript.txt` — что модель из этого звука разобрала, с временными
  метками, чтобы сопоставить конкретную фразу с конкретным местом записи;
* `summary.txt` — сколько голосовых пакетов пришло и сколько потерялось по
  дороге, отдельно на каждого говорящего (см. `PacketLossTracker`).

Модуль ничего не знает ни о Discord, ни о Vosk: на вход — целочисленный
идентификатор говорящего, сырые PCM-байты и порядковые номера пакетов.
Включается переменной окружения `SPEECH_DEBUG_DIR` (см.
`bot.config.Config.speech_debug_dir`) и по умолчанию выключен — постоянно
писать на диск чужую речь недопустимо (см. предупреждение о приватности в
README).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import wave
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Формат записи совпадает с форматом, который принимает распознавание
# (см. `bot.speech.SAMPLE_RATE`): смысл записи именно в том, чтобы на диске
# оказался тот же звук, что и в модели, байт в байт.
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1

# Как часто накопленный звук сбрасывается на диск. Запись идёт не на каждый
# чанк, а пачками: сам по себе `wave.Wave_write.writeframes` блокирующий, и
# дёргать его из потока event loop на каждые 20 мс речи значило бы ради
# отладки подтормаживать воспроизведение музыки.
_FLUSH_INTERVAL_SECONDS = 2.0

# Предел записи на одного говорящего. Отладочная запись легко переживает
# того, кто её включил: 16 кГц моно — это примерно 1.9 МБ в минуту на
# человека, и забытый на ночь флаг съел бы гигабайты. По достижении предела
# запись этого говорящего останавливается с предупреждением в лог, а бот
# продолжает работать как ни в чём не бывало.
DEFAULT_MAX_SECONDS = 600.0


def _timestamp() -> str:
    """Метка времени для имени каталога сеанса — в UTC, чтобы записи сортировались по порядку."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def _unique_session_dir(root: Path) -> Path:
    """Подбирает свободное имя каталога сеанса, не затирая уже существующий.

    Метка времени точна до секунды, а два сеанса подряд (отключение и
    немедленное переподключение к голосовому каналу — обычное дело)
    укладываются в одну секунду легко. Без подбора имени второй сеанс
    дописался бы в файлы первого, и разбирать пришлось бы кашу из двух
    записей.
    """
    base = _timestamp()
    candidate = root / base
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base}-{suffix}"
        suffix += 1
    return candidate


class PacketLossTracker:
    """Считает потерянные голосовые пакеты по разрывам в их порядковых номерах.

    Порядковый номер RTP растёт на единицу с каждым ОТПРАВЛЕННЫМ пакетом,
    поэтому пропуск в номерах — это именно потеря по дороге, а не пауза в
    речи: когда человек молчит, отправитель просто не шлёт пакетов, и
    следующий номер всё равно оказывается соседним. Это и делает счётчик
    однозначным ответом на вопрос «виновата ли связь»: доля потерь в
    единицы процентов уже рвёт слова и сбивает распознавание.

    Номер 16-битный и переполняется примерно каждые 22 минуты непрерывной
    речи; переход через границу считается нормой, а не потерей 65 тысяч
    пакетов (см. `note`).
    """

    _SEQUENCE_MODULO = 1 << 16
    # Разрыв больше этого считается переполнением счётчика или сменой
    # потока, а не потерей: реальные потери идут десятками пакетов, а не
    # тысячами, и без этой отсечки одно переполнение приписало бы связи
    # шестьдесят тысяч несуществующих потерь.
    _MAX_PLAUSIBLE_GAP = 1000

    def __init__(self) -> None:
        """Заводит пустые счётчики: говорящие добавляются по мере того, как заговорят."""
        self._expected: dict[int, int] = {}
        self._received: dict[int, int] = {}
        self._lost: dict[int, int] = {}

    def note(self, speaker_id: int, sequence: int) -> None:
        """Учитывает очередной пришедший пакет говорящего по его порядковому номеру."""
        self._received[speaker_id] = self._received.get(speaker_id, 0) + 1
        expected = self._expected.get(speaker_id)
        if expected is not None:
            gap = (sequence - expected) % self._SEQUENCE_MODULO
            if 0 < gap <= self._MAX_PLAUSIBLE_GAP:
                self._lost[speaker_id] = self._lost.get(speaker_id, 0) + gap
        self._expected[speaker_id] = (sequence + 1) % self._SEQUENCE_MODULO

    def drop_speaker(self, speaker_id: int) -> None:
        """Забывает говорящего — например, когда он вышел из голосового канала."""
        self._expected.pop(speaker_id, None)

    def stats(self, speaker_id: int) -> tuple[int, int]:
        """Возвращает пару «принято пакетов, потеряно пакетов» для одного говорящего."""
        return self._received.get(speaker_id, 0), self._lost.get(speaker_id, 0)

    def speakers(self) -> tuple[int, ...]:
        """Идентификаторы всех говорящих, о которых что-то известно."""
        return tuple(sorted(self._received))

    def report(self) -> str:
        """Человекочитаемый отчёт о потерях — попадает в `summary.txt` и в лог."""
        if not self._received:
            return "Ни одного голосового пакета не принято."
        lines = []
        for speaker_id in self.speakers():
            received, lost = self.stats(speaker_id)
            total = received + lost
            share = (lost / total * 100) if total else 0.0
            verdict = "связь в порядке" if share < 1.0 else "потери мешают распознаванию"
            lines.append(
                f"говорящий {speaker_id}: принято {received}, потеряно {lost} "
                f"({share:.1f}% — {verdict})"
            )
        return "\n".join(lines)


class SpeechRecorder:
    """Пишет на диск звук, уходящий в распознавание, вместе с расшифровкой и статистикой.

    Один экземпляр на сеанс отладки: при `start()` заводится отдельный
    каталог с меткой времени, внутри — по WAV-файлу на каждого говорящего,
    общий `transcript.txt` и `summary.txt` с итогом. Разные сеансы не
    перемешиваются, поэтому запись можно включить, воспроизвести проблему,
    выключить и разбирать один конкретный каталог.

    Звук копится в памяти и сбрасывается на диск пачками раз в
    `_FLUSH_INTERVAL_SECONDS` отдельным потоком (см. `_flush_loop`): запись
    в файл блокирующая, и делать её в потоке event loop на каждые 20 мс
    речи значило бы ради отладки подтормаживать музыку.
    """

    def __init__(
        self,
        directory: str,
        *,
        max_seconds: float = DEFAULT_MAX_SECONDS,
        loss_tracker: PacketLossTracker | None = None,
    ) -> None:
        """Запоминает каталог и предел записи; диска не касается до `start()`."""
        self._root = Path(directory)
        self._max_bytes = int(max_seconds * SAMPLE_RATE * SAMPLE_WIDTH)
        self._loss = loss_tracker if loss_tracker is not None else PacketLossTracker()

        self._session_dir: Path | None = None
        self._writers: dict[int, wave.Wave_write] = {}
        self._buffers: dict[int, bytearray] = {}
        self._written: dict[int, int] = {}
        self._limit_logged: set[int] = set()
        self._transcript: list[str] = []
        self._started_at = 0.0
        self._flush_task: asyncio.Task[None] | None = None

    @property
    def loss_tracker(self) -> PacketLossTracker:
        """Счётчик потерь пакетов — заполняется Discord-слоем приёма звука."""
        return self._loss

    @property
    def session_dir(self) -> Path | None:
        """Каталог текущего сеанса записи либо None, если запись не начиналась."""
        return self._session_dir

    async def start(self) -> None:
        """Создаёт каталог сеанса и запускает фоновый сброс на диск. Идемпотентен."""
        if self._session_dir is not None:
            return
        session_dir = await asyncio.to_thread(self._make_session_dir)
        # Сеанс начинается с чистого листа: иначе второй сеанс унаследовал
        # бы израсходованный предел записи от первого (и не записал бы ни
        # байта) и повторил бы в расшифровке его фразы.
        self._written.clear()
        self._limit_logged.clear()
        self._buffers.clear()
        self._transcript.clear()
        self._session_dir = session_dir
        self._started_at = time.monotonic()
        self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop())
        logger.warning(
            "Включена отладочная запись речи: всё, что слышит бот, пишется в %s", session_dir
        )

    def _make_session_dir(self) -> Path:
        """Синхронно создаёт каталог сеанса. Вызывать только из отдельного потока."""
        session_dir = _unique_session_dir(self._root)
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir

    def write(self, speaker_id: int, pcm: bytes) -> None:
        """Ставит очередной кусок звука говорящего в очередь на запись. Не блокирует.

        Вызывать из потока event loop: метод только копит байты в памяти,
        сам файл пишется фоновой задачей (см. `_flush_loop`).
        """
        if self._session_dir is None or not pcm:
            return
        if self._written.get(speaker_id, 0) >= self._max_bytes:
            if speaker_id not in self._limit_logged:
                self._limit_logged.add(speaker_id)
                logger.warning(
                    "Отладочная запись речи говорящего %s остановлена: достигнут предел "
                    "(%.0f минут). Поднимите SPEECH_DEBUG_MAX_SECONDS, если нужно больше.",
                    speaker_id,
                    self._max_bytes / (SAMPLE_RATE * SAMPLE_WIDTH) / 60,
                )
            return
        buffer = self._buffers.get(speaker_id)
        if buffer is None:
            buffer = bytearray()
            self._buffers[speaker_id] = buffer
        buffer.extend(pcm)
        self._written[speaker_id] = self._written.get(speaker_id, 0) + len(pcm)

    def note_phrase(self, speaker_id: int, text: str) -> None:
        """Записывает распознанную фразу в расшифровку с отметкой времени от начала сеанса."""
        if self._session_dir is None:
            return
        elapsed = time.monotonic() - self._started_at
        self._transcript.append(f"[{elapsed:7.1f} с] говорящий {speaker_id}: {text}")

    async def close(self) -> None:
        """Дописывает остатки на диск, сохраняет расшифровку и итог. Идемпотентен."""
        if self._session_dir is None:
            return
        task = self._flush_task
        self._flush_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        session_dir = self._session_dir
        self._session_dir = None
        report = self._loss.report()
        await asyncio.to_thread(self._finish_sync, session_dir, report)
        logger.warning("Отладочная запись речи сохранена в %s\n%s", session_dir, report)

    async def _flush_loop(self) -> None:
        """Периодически сбрасывает накопленный звук на диск, пока задача не отменена."""
        while True:
            await asyncio.sleep(_FLUSH_INTERVAL_SECONDS)
            await self._flush_once()

    async def _flush_once(self) -> None:
        """Забирает накопленные буферы и пишет их в файлы отдельным потоком."""
        if self._session_dir is None:
            return
        pending = {
            speaker_id: bytes(buffer) for speaker_id, buffer in self._buffers.items() if buffer
        }
        if not pending:
            return
        for buffer in self._buffers.values():
            buffer.clear()
        try:
            await asyncio.to_thread(self._write_sync, self._session_dir, pending)
        except OSError:
            # Отладочная запись не должна ронять приём голоса: если диск
            # переполнен или каталог недоступен, бот обязан продолжать
            # играть музыку и слушать команды.
            logger.exception("Не удалось записать отладочный звук — запись пропущена")

    def _write_sync(self, session_dir: Path, pending: dict[int, bytes]) -> None:
        """Синхронно дописывает звук в WAV-файлы. Вызывать только из отдельного потока."""
        for speaker_id, pcm in pending.items():
            writer = self._writers.get(speaker_id)
            if writer is None:
                path = session_dir / f"speaker-{speaker_id}.wav"
                writer = wave.open(str(path), "wb")
                writer.setnchannels(CHANNELS)
                writer.setsampwidth(SAMPLE_WIDTH)
                writer.setframerate(SAMPLE_RATE)
                self._writers[speaker_id] = writer
            writer.writeframes(pcm)

    def _finish_sync(self, session_dir: Path, report: str) -> None:
        """Синхронно дописывает остатки, закрывает файлы и сохраняет расшифровку и итог."""
        pending = {
            speaker_id: bytes(buffer) for speaker_id, buffer in self._buffers.items() if buffer
        }
        self._buffers.clear()
        with contextlib.suppress(OSError):
            self._write_sync(session_dir, pending)

        for writer in self._writers.values():
            with contextlib.suppress(Exception):
                writer.close()
        self._writers.clear()

        with contextlib.suppress(OSError):
            transcript = "\n".join(self._transcript) or "Ни одной фразы не распознано."
            (session_dir / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
            (session_dir / "summary.txt").write_text(
                "Статистика приёма голосовых пакетов\n"
                "==================================\n"
                f"{report}\n\n"
                "Как читать: разрыв в порядковых номерах пакетов — это потеря по\n"
                "дороге, а не пауза в речи. До 1% потерь распознавание почти не\n"
                "замечает; выше — из слов выпадают куски, и модель тут ни при чём.\n"
                "Послушайте speaker-<id>.wav: если звук чистый, а текст в\n"
                "transcript.txt неверный — дело в модели, попробуйте модель\n"
                "покрупнее. Если звук рваный — дело в связи.\n",
                encoding="utf-8",
            )
