"""Офлайн-распознавание русской речи через Vosk — модуль ничего не знает о Discord.

На вход — сырые PCM-байты 16 кГц/моно/16 бит одного говорящего (см.
`SAMPLE_RATE`), на выход — распознанный текст законченных фраз. Формат
входа выбран под модель Vosk (`models/vosk-model-small-ru-0.22` по
умолчанию, см. README про то, как её скачать) — она ничего другого не
понимает. Ресемплинг из формата, в котором звук реально приходит (в
проекте — 48 кГц/стерео от Discord), сюда не входит и не должен: этим
занимается вызывающий Discord-слой (см. `bot.listening`), а сам модуль
остаётся пригодным для любого источника PCM нужного формата, не только для
Discord.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from vosk import KaldiRecognizer, Model, SetLogLevel

from bot.errors import SpeechModelUnavailableError

logger = logging.getLogger(__name__)

# Формат, который ожидает модель Vosk: PCM 16 бит, моно, 16 кГц.
SAMPLE_RATE = 16000

# Путь к модели по умолчанию — тот же, что и в README (раздел про
# распознавание речи), совпадает с bot.config._DEFAULT_SPEECH_MODEL_PATH.
DEFAULT_MODEL_PATH = "models/vosk-model-small-ru-0.22"

# Как часто фоновая задача проверяет говорящих на бездействие. Не слишком
# часто (не нагружать впустую) и не слишком редко (не копить память надолго
# после того, как человек замолчал или отошёл от микрофона, не покинув
# канал явно — см. докстринг SpeechRecognizer про drop_speaker).
_CLEANUP_INTERVAL_SECONDS = 30.0

# Через сколько секунд без единого кадра от говорящего его распознаватель
# считается устаревшим и освобождается фоновой уборкой.
DEFAULT_SPEAKER_IDLE_TIMEOUT = 120.0


@dataclass(slots=True)
class _SpeakerState:
    """Состояние распознавания одного говорящего: свой `KaldiRecognizer` и время последней речи.

    `KaldiRecognizer` хранит состояние текущей, ещё не завершённой фразы —
    смешивать в одном объекте кадры разных людей значило бы получить кашу
    из двух голосов вместо текста, поэтому у каждого говорящего строго свой
    экземпляр (см. докстринг SpeechRecognizer).
    """

    recognizer: KaldiRecognizer
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_active: float = field(default_factory=time.monotonic)


def _load_model_sync(model_path: str) -> Model:
    """Синхронно грузит модель Vosk с диска. Вызывать только из потока (см. `ensure_ready`).

    Путь проверяется на существование заранее, до вызова `Model(...)`: без
    этой проверки нативная библиотека Vosk при отсутствующей модели не
    только бросает исключение (его можно поймать), но и сама печатает
    ERROR-строку в stderr в обход logging — с проверкой заранее это
    сообщение не появляется вовсе, а ошибка остаётся понятной и на русском.
    """
    if not Path(model_path).is_dir():
        raise SpeechModelUnavailableError(
            f"Модель распознавания речи не найдена по пути {model_path!r}.",
            user_message=(
                "Модель распознавания речи не найдена на диске. Скачайте её "
                "по инструкции в README (раздел про распознавание речи) и "
                f"распакуйте в «{model_path}»."
            ),
        )
    # Иначе нативная библиотека Vosk сыплет отладочными сообщениями в stderr
    # при каждой загрузке модели и почти на каждую обработанную фразу — не
    # нужно в обычной работе бота.
    SetLogLevel(-1)
    try:
        return Model(model_path)
    except Exception as exc:
        raise SpeechModelUnavailableError(
            f"Не удалось загрузить модель распознавания речи из {model_path!r}: {exc}",
            user_message="Не удалось загрузить модель распознавания речи, подробности в логах.",
        ) from exc


def _final_result(recognizer: KaldiRecognizer) -> str | None:
    """Досрочно закрывает текущую фразу и возвращает её текст, если он непустой.

    Выполняется в отдельном потоке (см. `SpeechRecognizer.flush`), хотя и
    стоит всего несколько миллисекунд: `KaldiRecognizer` — нативный объект
    с состоянием, и обращаться к нему из разных потоков одновременно
    нельзя, поэтому сброс идёт через тот же лок и тот же пул, что и
    обычная обработка.
    """
    result = json.loads(recognizer.FinalResult())
    text = result.get("text", "").strip()
    return text or None


def _process_chunk(recognizer: KaldiRecognizer, pcm: bytes) -> str | None:
    """Кормит распознаватель PCM-чанком; возвращает текст, если фраза именно на нём завершилась.

    Выполняется в отдельном потоке (см. `SpeechRecognizer.feed`): сам вызов
    `AcceptWaveform` нагружает процессор и не должен занимать поток event
    loop. `AcceptWaveform` возвращает True в момент, когда Vosk считает
    фразу законченной (обычно по паузе в речи) — только тогда есть смысл
    забирать `Result()`; на промежуточных чанках, из которых фраза ещё не
    сложилась, возвращается None.
    """
    finished = recognizer.AcceptWaveform(pcm)
    if not finished:
        return None
    result = json.loads(recognizer.Result())
    text = result.get("text", "").strip()
    return text or None


class SpeechRecognizer:
    """Распознаёт офлайн русскую речь по PCM 16 кГц/моно/16 бит через Vosk.

    Модель Vosk грузится один раз (`ensure_ready`) и переиспользуется для
    всех говорящих и всех фраз — загрузка занимает заметное время и память,
    повторять её на каждую фразу нельзя. А вот `KaldiRecognizer` — свой на
    каждого говорящего (ключ — произвольный `int`, в проекте это Discord
    user id): он хранит состояние ещё не законченной фразы, и один объект
    на всех означал бы, что слова разных людей перемешиваются в один поток.

    Само распознавание (`AcceptWaveform`) нагружает процессор и не должно
    занимать поток event loop — `feed()` уносит его в `asyncio.to_thread`
    (пул потоков по умолчанию, размер которого подбирает сам asyncio).
    Конкурентные вызовы `feed()` для ОДНОГО говорящего сериализуются его
    собственным `asyncio.Lock` в `_SpeakerState`: `KaldiRecognizer` — это
    обёртка над нативным объектом с состоянием, и параллельные вызовы над
    одним и тем же экземпляром из разных потоков пула ничем не защищены.
    Разные говорящие при этом обрабатываются по-настоящему параллельно —
    у каждого свой объект и свой поток пула, они друг другу не мешают.

    Говорящие, надолго замолчавшие (никто не звал `feed()` дольше
    `speaker_idle_timeout`), убираются фоновой задачей `_cleanup_loop` —
    иначе память росла бы, пока бот работает, никогда не забывая тех, кто
    уже вышел из канала или просто больше не говорит. Явный уход из
    голосового канала — это Discord-событие, а модуль о Discord не знает
    вообще, поэтому для него наружу отдан `drop_speaker` — вызывающий
    Discord-слой убирает состояние сразу этим методом, не дожидаясь
    таймаута (см. `bot.listening.GuildListener`).
    """

    def __init__(
        self,
        *,
        model_path: str = DEFAULT_MODEL_PATH,
        speaker_idle_timeout: float = DEFAULT_SPEAKER_IDLE_TIMEOUT,
    ) -> None:
        """Запоминает путь к модели и таймаут бездействия; саму модель не трогает.

        Загрузка — только внутри `ensure_ready()`, чтобы конструктор
        оставался мгновенным и не блокировал даже поток своего вызова.
        """
        self._model_path = model_path
        self._speaker_idle_timeout = speaker_idle_timeout
        self._model: Model | None = None
        self._load_lock = asyncio.Lock()
        self._speakers: dict[int, _SpeakerState] = {}
        self._cleanup_task: asyncio.Task[None] | None = None

    @property
    def is_ready(self) -> bool:
        """Признак того, что модель уже загружена и распознаватель готов принимать `feed()`."""
        return self._model is not None

    async def ensure_ready(self) -> None:
        """Загружает модель Vosk один раз, в отдельном потоке — не блокируя event loop.

        Идемпотентен: повторные вызовы после успешной загрузки — no-op.
        Конкурентные вызовы до первой успешной загрузки ждут одну и ту же
        попытку под `_load_lock`, а не грузят модель параллельно несколько
        раз (лишние копии в памяти на время загрузки). Бросает
        `SpeechModelUnavailableError`, если модели нет на диске или она
        повреждена — это доменная ошибка (см. `bot.errors.BotError`),
        вызывающий Discord-слой обязан превратить её в понятный текст
        пользователю, а не падать стектрейсом.
        """
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            self._model = await asyncio.to_thread(_load_model_sync, self._model_path)
            self._cleanup_task = asyncio.get_running_loop().create_task(self._cleanup_loop())

    async def feed(self, speaker_id: int, pcm: bytes) -> str | None:
        """Кормит очередной PCM-чанк говорящего; возвращает текст, когда фраза только что закончена.

        `pcm` — сырые байты 16 кГц/моно/16 бит (см. `SAMPLE_RATE`);
        ресемплинг из формата источника звука делает вызывающий код, этот
        метод формат не проверяет и ничего о его происхождении не знает.
        Требует предварительного успешного `ensure_ready()` — без
        загруженной модели создавать распознаватель не из чего.
        """
        if self._model is None:
            raise SpeechModelUnavailableError(
                "SpeechRecognizer.feed() вызван до ensure_ready() — модель ещё не загружена."
            )
        state = self._get_or_create_speaker(speaker_id)
        async with state.lock:
            text = await asyncio.to_thread(_process_chunk, state.recognizer, pcm)
            state.last_active = time.monotonic()
        return text

    async def flush(self, speaker_id: int) -> str | None:
        """Досрочно закрывает фразу говорящего и возвращает её текст. Без модели — `None`.

        Нужен потому, что сама по себе фраза закрывается только по тишине,
        а тишина в голосовом канале заканчивается раньше, чем распознавание
        успевает счесть фразу законченной: Discord после того, как человек
        замолчал, отправляет около сотни миллисекунд тишины и перестаёт
        слать пакеты вовсе, тогда как распознаванию нужно порядка
        полусекунды. Без досрочного сброса текст команды появлялся бы
        только в тот момент, когда человек заговорит СНОВА, — то есть
        неизвестно когда (см. `bot.listening.GuildListener._pump_speaker`,
        который и вызывает этот метод по паузе).

        Идемпотентен: повторный сброс без новой речи вернёт `None`, потому
        что закрывать уже нечего.
        """
        state = self._speakers.get(speaker_id)
        if state is None:
            return None
        async with state.lock:
            text = await asyncio.to_thread(_final_result, state.recognizer)
            state.last_active = time.monotonic()
        return text

    def _get_or_create_speaker(self, speaker_id: int) -> _SpeakerState:
        """Возвращает состояние говорящего, создавая его при первом обращении.

        Модель к этому моменту уже точно загружена (проверено в `feed`),
        а создание `KaldiRecognizer` — в отличие от загрузки модели — дёшево
        и не требует ухода в отдельный поток.
        """
        state = self._speakers.get(speaker_id)
        if state is None:
            assert self._model is not None
            state = _SpeakerState(recognizer=KaldiRecognizer(self._model, SAMPLE_RATE))
            self._speakers[speaker_id] = state
        return state

    def drop_speaker(self, speaker_id: int) -> None:
        """Немедленно освобождает распознаватель говорящего — например, при выходе из канала.

        Идемпотентен: повторный вызов для уже отсутствующего `speaker_id` —
        no-op. Синхронный и не уходит в поток: удаление из словаря и
        последующее освобождение `KaldiRecognizer` сборщиком мусора (у него
        нет отдельного метода закрытия) достаточно дёшевы для потока event
        loop.
        """
        self._speakers.pop(speaker_id, None)

    async def _cleanup_loop(self) -> None:
        """Периодически убирает говорящих, не подававших речь дольше `speaker_idle_timeout`."""
        while True:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            now = time.monotonic()
            stale = [
                speaker_id
                for speaker_id, state in self._speakers.items()
                if now - state.last_active >= self._speaker_idle_timeout
            ]
            for speaker_id in stale:
                self.drop_speaker(speaker_id)
                logger.debug(
                    "Распознаватель речи говорящего %s убран по таймауту бездействия (%.0f с)",
                    speaker_id,
                    self._speaker_idle_timeout,
                )

    async def close(self) -> None:
        """Останавливает фоновую уборку и освобождает состояние всех говорящих. Идемпотентен.

        Модель (`self._model`) не сбрасывается: `SpeechRecognizer` в
        проекте живёт всё время работы кога (см. `bot.cogs.listen.ListenCog`),
        `close()` вызывается только при выгрузке бота, а не между сеансами
        `/listen`/`/listen_stop` — так что модель остаётся загруженной и
        готовой к переиспользованию, если распознавание запустят снова
        до полного перезапуска бота.
        """
        task = self._cleanup_task
        self._cleanup_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._speakers.clear()
