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
import re

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


# Числительные 0..19 (включая) записаны отдельно, а не выведены из десятков
# и единиц: у русского языка «одиннадцать»..«девятнадцать» — не «десять
# один» и т. п., а самостоятельные слова, и собирать их арифметикой нельзя.
_ONES_WORDS: dict[int, str] = {
    0: "ноль",
    1: "один",
    2: "два",
    3: "три",
    4: "четыре",
    5: "пять",
    6: "шесть",
    7: "семь",
    8: "восемь",
    9: "девять",
    10: "десять",
    11: "одиннадцать",
    12: "двенадцать",
    13: "тринадцать",
    14: "четырнадцать",
    15: "пятнадцать",
    16: "шестнадцать",
    17: "семнадцать",
    18: "восемнадцать",
    19: "девятнадцать",
}

# Круглые десятки 20..90 — к ним при необходимости приписывается единица из
# `_ONES_WORDS` («сорок» + «пять» → «сорок пять»), см. `_int_to_words`.
_TENS_WORDS: dict[int, str] = {
    20: "двадцать",
    30: "тридцать",
    40: "сорок",
    50: "пятьдесят",
    60: "шестьдесят",
    70: "семьдесят",
    80: "восемьдесят",
    90: "девяносто",
}

# Слова цифр для поразрядного чтения чисел вне 0..100 (см. `_number_words`) —
# точность произношения таких чисел не критична, важно не падать на них.
_DIGIT_WORDS: dict[str, str] = {
    "0": "ноль",
    "1": "один",
    "2": "два",
    "3": "три",
    "4": "четыре",
    "5": "пять",
    "6": "шесть",
    "7": "семь",
    "8": "восемь",
    "9": "девять",
}


def _int_to_words(value: int) -> str:
    """Переводит целое число 0..100 в русские слова."""
    if value in _ONES_WORDS:
        return _ONES_WORDS[value]
    if value == 100:
        return "сто"
    tens, ones = divmod(value, 10)
    word = _TENS_WORDS[tens * 10]
    return f"{word} {_ONES_WORDS[ones]}" if ones else word


def _number_words(digits: str) -> str:
    """Озвучивает цепочку цифр: 0..100 — словом, всё прочее — поразрядно.

    Поразрядное чтение — сознательно грубое приближение (см. докстринг
    `speakable_text`), но оно покрывает и большие числа («2026»), и числа с
    ведущим нулём («007», где обычное преобразование в int исказило бы
    смысл, прочитав их как «семь»).
    """
    if len(digits) > 1 and digits[0] == "0":
        return " ".join(_DIGIT_WORDS[digit] for digit in digits)
    value = int(digits)
    if value <= 100:
        return _int_to_words(value)
    return " ".join(_DIGIT_WORDS[digit] for digit in digits)


# Двух- и трёхбуквенные сочетания транслитерируются целиком, а не по одной
# букве, — так практическое звучание ближе к оригиналу («sh» — это «ш», а не
# «сх»). Список отсортирован от более длинных сочетаний к более коротким:
# `_transliterate` проверяет их по порядку и должна встретить «shch» раньше
# «sh», иначе более длинное сочетание никогда не сработает.
_LATIN_DIGRAPHS: tuple[tuple[str, str], ...] = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("tch", "ч"),
    ("sh", "ш"),
    ("ch", "ч"),
    ("kh", "х"),
    ("ph", "ф"),
    ("th", "т"),
    ("ts", "ц"),
    ("tz", "ц"),
    ("zh", "ж"),
    ("ya", "я"),
    ("ja", "я"),
    ("yu", "ю"),
    ("ju", "ю"),
    ("yo", "ё"),
    ("ye", "е"),
    ("qu", "кв"),
    ("ck", "к"),
    ("oo", "у"),
    ("ee", "и"),
)

# Однобуквенное соответствие — на случай, когда ни одно сочетание выше не
# подошло. Огрублённое и однозначное: цель — что-то похожее на звучание
# исходного слова, а не орфографически точная транслитерация (см. докстринг
# `speakable_text`).
_LATIN_LETTERS: dict[str, str] = {
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "й",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "й",
    "z": "з",
}


def _transliterate(word: str) -> str:
    """Практическая транслитерация латинского слова (в нижнем регистре) в кириллицу по звучанию."""
    result: list[str] = []
    index = 0
    length = len(word)
    while index < length:
        for pattern, replacement in _LATIN_DIGRAPHS:
            if word.startswith(pattern, index):
                result.append(replacement)
                index += len(pattern)
                break
        else:
            result.append(_LATIN_LETTERS.get(word[index], ""))
            index += 1
    return "".join(result)


# Что распознаётся и озвучивается при разборе `speakable_text`: цепочка
# цифр, цепочка латинских букв, либо одиночный символ из допустимого
# «прочего» — русская буква, пробел, запятая или точка. Всё остальное
# (тире, кавычки, скобки, эмодзи и т. п.) не попадает ни под одну группу и
# просто пропускается — `re.finditer` не возвращает промежутки между
# совпадениями.
_SPEAKABLE_TOKEN = re.compile(r"(?P<num>\d+)|(?P<latin>[a-zA-Z]+)|(?P<keep>[а-яА-ЯёЁ ,.])")


def _speakable_token(match: re.Match[str]) -> str:
    """Озвучивает найденный токен: число словами, латиницу — транслитерацией, а прочее — как есть.

    Ровно одна из трёх именованных групп `_SPEAKABLE_TOKEN` всегда
    заполнена — по устройству самого регулярного выражения.
    """
    digits = match.group("num")
    if digits is not None:
        return _number_words(digits)
    latin = match.group("latin")
    if latin is not None:
        return _transliterate(latin.lower())
    return match.group("keep")


def speakable_text(text: str) -> str:
    """Приводит текст к тому, что синтез `vosk_tts` умеет выговорить, не бросая исключений.

    Модель синтеза знает только русские буквы (плюс пробел, запятую и
    точку для пауз и интонации) и падает исключением на первом же
    незнакомом символе — арабской цифре, латинской букве, тире, эмодзи и
    т. п. (см. `TextToSpeech.synthesize`, где раньше `'5'` и `'i'` роняли
    весь синтез). Раньше это ломало уже выпущенные возможности бота:
    `/say hello`, «повтори» на английском и объявление названий треков, где
    латиница на Яндекс.Музыке — обычное дело.

    Правила приведения:
    - цифры превращаются в русские слова («50» → «пятьдесят»); для 0..100 —
      точно, для более длинных чисел — поразрядно («2026» → «два ноль два
      шесть»): точность произношения больших чисел не критична, важно не
      падать (см. `_number_words`);
    - латиница транслитерируется в кириллицу по звучанию, практически и
      огрублённо («Nirvana» → что-то вроде «нирвана») — задача в том, чтобы
      бот произнёс похожее, а не молчал с ошибкой (см. `_transliterate`);
    - всё прочее, что не русская буква, не пробел, не запятая и не точка,
      выбрасывается, а лишние пробелы схлопываются.

    Если после очистки не осталось ничего — возвращается пустая строка:
    вызывающий код (`TextToSpeech.synthesize`) уже умеет превращать пустой
    текст в пустой звук, а не в ошибку.

    Функция обязана быть чистой и не бросать исключений ни на каком входе —
    от неё зависит вообще любая произнесённая ботом фраза (`/say`,
    «повтори», голосовые ответы из `bot.voice_replies`), и одна неучтённая
    мелочь не должна ронять синтез целиком.
    """
    try:
        spoken = "".join(_speakable_token(match) for match in _SPEAKABLE_TOKEN.finditer(text))
        return " ".join(spoken.split())
    except Exception:
        return ""


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
        у которого есть чем ответить человеку. `speakable_text` вызывается
        здесь же, внутри `synthesize`, а не в каждом отдельном вызывающем
        коде (`/say`, «повтори», голосовые ответы) — так застрахованы сразу
        все пути произнесения разом, а не каждый по отдельности: цифры и
        латиница иначе роняли бы синтез исключением (см. её докстринг).
        """
        prepared = speakable_text(clean_text(text))
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
