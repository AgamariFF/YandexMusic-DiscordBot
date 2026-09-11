"""Офлайн-синтез русской речи через vosk-tts — модуль ничего не знает о Discord.

На вход — текст, на выход — сырой PCM в формате голосового канала Discord
(48 кГц, стерео, 16 бит). Приведение к этому формату живёт здесь, а не в
вызывающем коде, по той же причине, по какой обратное преобразование живёт
в `bot.listening`: формат Discord — деталь одного конкретного потребителя,
и наружу модуль отдаёт уже готовое к отправке, не заставляя каждого
вызывающего помнить про ресемплинг.

Синтез парный к распознаванию из `bot.speech`: та же идея офлайн-работы
(звук и текст не покидают машину, на которой запущен бот) и та же модель
поведения — тяжёлая модель грузится один раз, сама работа уходит в
отдельный поток, чтобы не занимать поток событий, пока играет музыка.

В отличие от модели распознавания, модель синтеза скачивается сама при
первом обращении (около 130 МБ, см. `DEFAULT_MODEL_NAME`) — так устроен
сам vosk-tts. Поэтому загрузка вынесена в явный `ensure_ready()`, а
вызывающий код обязан предупредить человека, что первая фраза потребует
ожидания (см. `bot.cogs.voice_control`).
"""

from __future__ import annotations

import asyncio
import logging

from bot.errors import SpeechSynthesisUnavailableError
from bot.nekto.audio import (
    DISCORD_CHANNELS,
    DISCORD_SAMPLE_RATE,
    DISCORD_SAMPLE_WIDTH,
    frame_to_pcm,
)

logger = logging.getLogger(__name__)

# Многоголосая русская модель: 129 МБ, синтезирует примерно в 18 раз быстрее
# реального времени. Проверена честно — синтезированная ею фраза была
# полностью разобрана нашим же распознаванием (7 слов из 7).
DEFAULT_MODEL_NAME = "vosk-model-tts-ru-0.7-multi"

# Голос по умолчанию у многоголосой модели. Значение подобрано на слух;
# у модели их пять (0..4), меняется настройкой TTS_SPEAKER_ID.
DEFAULT_SPEAKER_ID = 2

# Частота, на которой модель отдаёт звук. Нужна для приведения к формату
# Discord и заведомо от него отличается — отсюда и ресемплинг ниже.
MODEL_SAMPLE_RATE = 22050

# Предел длины произносимого текста. Ограничение не техническое, а
# человеческое: бот занимает голосовой канал на всё время фразы и глушит
# музыку, поэтому «повтори за мной» не должно превращаться в зачитывание
# простыни текста на несколько минут.
MAX_TEXT_LENGTH = 300


def _synthesize_sync(synth, text: str, speaker_id: int) -> bytes:
    """Синтезирует речь и приводит её к формату Discord. Вызывать только из потока.

    Ресемплинг делается здесь же, а не отдельным шагом на потоке событий:
    и синтез, и приведение формата — счётная работа, и разносить их по
    разным потокам незачем.
    """
    import av

    audio = synth.synth_audio(text, speaker_id=speaker_id)
    raw = audio.tobytes()
    if not raw:
        return b""

    frame = av.AudioFrame(format="s16", layout="mono", samples=len(raw) // 2)
    frame.sample_rate = MODEL_SAMPLE_RATE
    memoryview(frame.planes[0])[:] = raw

    resampler = av.AudioResampler(
        format="s16", layout="stereo", rate=DISCORD_SAMPLE_RATE
    )
    chunks = [frame_to_pcm(resampled) for resampled in resampler.resample(frame)]
    # Хвост фильтра: без него у каждой фразы обрезался бы самый конец.
    chunks.extend(frame_to_pcm(resampled) for resampled in resampler.resample(None))
    return b"".join(chunks)


def clean_text(text: str) -> str:
    """Готовит произносимый текст: схлопывает пробелы и обрезает по `MAX_TEXT_LENGTH`."""
    return " ".join(text.split())[:MAX_TEXT_LENGTH]


class TextToSpeech:
    """Синтезирует русскую речь и отдаёт её готовой к отправке в голосовой канал Discord.

    Модель грузится один раз (`ensure_ready`) и переиспользуется: загрузка
    занимает секунды и сотни мегабайт памяти, повторять её на каждую фразу
    нельзя. Сам синтез уходит в отдельный поток — он счётный и на потоке
    событий заикалась бы музыка.

    Конкурентные вызовы `synthesize()` сериализуются общим локом: `Synth` —
    обёртка над нативной моделью с внутренним состоянием, и одновременные
    обращения к одному экземпляру из разных потоков ничем не защищены. Для
    бота это не ограничение: фразы всё равно произносятся по очереди, потому
    что голосовой канал один.
    """

    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL_NAME,
        speaker_id: int = DEFAULT_SPEAKER_ID,
    ) -> None:
        """Запоминает имя модели и голос; саму модель не трогает до `ensure_ready()`."""
        self._model_name = model_name
        self._speaker_id = speaker_id
        self._synth = None
        self._load_lock = asyncio.Lock()
        self._synth_lock = asyncio.Lock()

    @property
    def is_ready(self) -> bool:
        """Признак того, что модель загружена и синтез готов принимать текст."""
        return self._synth is not None

    async def ensure_ready(self) -> None:
        """Загружает модель синтеза один раз, в отдельном потоке. Идемпотентен.

        При первом запуске модель скачивается (около 130 МБ) — это может
        занять заметное время, поэтому вызывающий код обязан предупредить
        человека. Все ошибки (нет пакета, нет сети, битая модель)
        сводятся к `SpeechSynthesisUnavailableError`: это доменная ошибка,
        и голосовая команда обязана превратить её в понятный текст, а не
        упасть стектрейсом.
        """
        if self._synth is not None:
            return
        async with self._load_lock:
            if self._synth is not None:
                return
            self._synth = await asyncio.to_thread(self._load_sync)

    def _load_sync(self):
        """Синхронно грузит модель синтеза. Вызывать только из отдельного потока."""
        try:
            from vosk_tts import Model, Synth
        except ImportError as exc:
            raise SpeechSynthesisUnavailableError(
                f"Пакет vosk-tts не установлен: {exc}",
                user_message=(
                    "Синтез речи недоступен: не установлен пакет vosk-tts. "
                    "Выполните «pip install -r requirements.txt» и перезапустите бота."
                ),
            ) from exc

        logger.info("Загрузка модели синтеза речи %s", self._model_name)
        try:
            return Synth(Model(model_name=self._model_name))
        except Exception as exc:
            raise SpeechSynthesisUnavailableError(
                f"Не удалось загрузить модель синтеза речи {self._model_name!r}: {exc}",
                user_message=(
                    "Не удалось загрузить модель синтеза речи, подробности в логах."
                ),
            ) from exc

    async def synthesize(self, text: str) -> bytes:
        """Синтезирует текст и возвращает PCM в формате Discord (48 кГц, стерео, 16 бит).

        Пустой текст даёт пустой результат, а не ошибку: решение о том, что
        делать с «нечего произносить», принимает вызывающий Discord-слой,
        у которого есть чем ответить человеку.
        """
        prepared = clean_text(text)
        if not prepared:
            return b""
        await self.ensure_ready()
        async with self._synth_lock:
            try:
                return await asyncio.to_thread(
                    _synthesize_sync, self._synth, prepared, self._speaker_id
                )
            except SpeechSynthesisUnavailableError:
                raise
            except Exception as exc:
                raise SpeechSynthesisUnavailableError(
                    f"Не удалось синтезировать речь: {exc}",
                    user_message="Не удалось произнести фразу, подробности в логах.",
                ) from exc

    @staticmethod
    def duration_seconds(pcm: bytes) -> float:
        """Длительность синтезированного PCM в секундах — для логов и подтверждений."""
        frame_size = DISCORD_CHANNELS * DISCORD_SAMPLE_WIDTH
        return len(pcm) / frame_size / DISCORD_SAMPLE_RATE if frame_size else 0.0
