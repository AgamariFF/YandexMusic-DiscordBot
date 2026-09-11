"""Разбор голосовых команд боту из текста, распознанного Vosk — без единого байта ввода-вывода.

На вход всегда приходит результат офлайн-распознавания русской речи моделью
Vosk (см. `bot.speech`): нижний регистр, без знаков препинания и заглавных
букв, слова через пробел, например `"катя следующий трек пожалуйста"`. Модуль
не знает ни о Discord, ни о Vosk, ни о звуке — он умеет ровно одно: превращать
такую строку в `VoiceCommand` либо в `None`, если во фразе нет обращения по
имени бота или команда не опознана. Это чистая функция без побочных
эффектов — её удобно тестировать юнит-тестами и незачем гонять через
асинхронный Discord-слой.

Падежи и формы слов распознаются сопоставлением по неизменяемому НАЧАЛУ
слова (основе), а не полноценным морфологическим анализом: словарь команд
маленький и закрытый (десяток действий, известных заранее), а подключать
ради него внешнюю библиотеку с словарями на десятки мегабайт — избыточно.
У огрубления есть цена: возможны ложные совпадения на словах с тем же
началом, что и основа команды (например основа «друг» из команды «другую
песню» совпадёт и со словом «дружище», если оно вдруг окажется в фразе). Для
закрытого набора команд, которые слышит голосовой бот, это осознанно
принятый компромисс.

Числительные (для команды `volume`) разобраны отдельно, явным словарём форм,
а не тем же приёмом с обрезанной основой: у числительных семейства «два» —
«два/двух/двум», «двадцать», «двенадцать», «двести» — общее начало «дв»,
и обрезанная основа неизбежно перепутала бы 2, 12, 20 и 200 между собой, а
ошибка в громкости — неприятный побочный эффект, которого стоит избежать
явным списком форм вместо префиксов.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

# Имя, на которое отзывается бот. Первым значимым словом фразы (после
# необязательных слов-заполнителей, см. `_FILLER_WORDS`) должно быть именно
# оно или его допустимая форма/опечатка — иначе фраза не считается
# обращением к боту (см. `_extract_command_body`).
WAKE_WORD = "катя"

VoiceAction = Literal[
    "pause", "resume", "skip", "stop", "wave", "search", "volume", "now_playing"
]


@dataclass(frozen=True, slots=True)
class VoiceCommand:
    """Голосовая команда боту, разобранная из текста Vosk.

    `query` заполняется у `search` (поисковый запрос) и у `wave`, если
    волна запрошена от конкретного трека/исполнителя. `volume_percent` и
    `volume_delta` относятся только к `volume`, и заполнено у неё ровно
    одно из двух — абсолютная громкость либо относительное изменение.
    У всех остальных действий все три поля — `None`.
    """

    action: VoiceAction
    query: str | None = None
    volume_percent: int | None = None
    volume_delta: int | None = None


# --- Обращение по имени -------------------------------------------------

# Падежи и уменьшительные формы имени, которые не укладываются в допуск
# Левенштейна ≤ 1 от «катя» (см. `_looks_like_wake_word`) — например
# «катей» отличается от «катя» сразу на замену буквы и вставку, а «катька»
# и её формы вообще содержат лишний слог.
_NAME_FORMS = frozenset(
    {"катя", "кати", "кате", "катю", "катей", "катька", "катьку", "катьки", "кать"}
)

# Слова-заполнители, которые допускаются перед именем и молча пропускаются.
# Само имя обязано быть первым значимым (не заполнителем) словом фразы.
_FILLER_WORDS = frozenset({"э", "а", "ну", "эй", "слушай", "окей", "ok"})

# Слова, извлечённые из текста: только буквы (кириллица и латиница — на
# случай мусора вроде «ok») и цифры, «ё» приравнена к «е» заранее в
# `_tokenize`, потому что Vosk отдаёт то так, то так одно и то же слово.
_WORD_PATTERN = re.compile(r"[a-zа-я0-9]+")


def _levenshtein(left: str, right: str) -> int:
    """Расстояние Левенштейна между двумя строками (минимум вставок/удалений/замен).

    Своя маленькая реализация вместо внешней библиотеки: используется ровно
    в одном месте — проверка опечатки в имени бота на расстоянии ≤ 1 (см.
    `_looks_like_wake_word`), тащить ради этого целую зависимость избыточно.
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    previous_row = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current_row = [i]
        for j, right_char in enumerate(right, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1
            substitute_cost = previous_row[j - 1] + (left_char != right_char)
            current_row.append(min(insert_cost, delete_cost, substitute_cost))
        previous_row = current_row
    return previous_row[-1]


def _looks_like_wake_word(word: str) -> bool:
    """Слово похоже на обращение «катя» — известная форма либо опечатка на одну букву.

    Модель Vosk маленькая и нередко слышит имя с ошибкой на одну букву,
    поэтому вместе с явным списком падежей и уменьшительных форм (см.
    `_NAME_FORMS`) принимается и произвольное слово на расстоянии
    Левенштейна ≤ 1 от «катя» — так опечатки вроде «хатя», «ката», «кая»
    распознаются, а далёкие по написанию слова вроде «катер», «каток»,
    «катать», «картина» (расстояние ≥ 2) остаются не похожими на имя.
    """
    return word in _NAME_FORMS or _levenshtein(word, WAKE_WORD) <= 1


def _tokenize(text: str) -> list[str]:
    """Приводит сырой текст к списку слов: без «ё», без пунктуации и без лишних пробелов.

    Vosk и так отдаёт нижний регистр без знаков препинания, но функция не
    полагается на это слепо (см. докстринг `parse_voice_command` про то, что
    разбор обязан не падать ни на каком входе) — регистр приводится ещё раз,
    а всё, что не буква и не цифра, регулярное выражение просто отбрасывает.
    """
    normalized = text.lower().replace("ё", "е")
    return _WORD_PATTERN.findall(normalized)


def _extract_command_body(words: list[str]) -> list[str] | None:
    """Ищет обращение по имени в начале фразы и возвращает слова, идущие после него.

    Перед именем допускаются слова-заполнители (см. `_FILLER_WORDS`), но само
    имя обязано быть первым значимым словом — если оно стоит в середине
    фразы, это не обращение к боту, а случайное упоминание похожего слова, и
    функция возвращает `None`, не заглядывая дальше.
    """
    for index, word in enumerate(words):
        if word in _FILLER_WORDS:
            continue
        if _looks_like_wake_word(word):
            return words[index + 1 :]
        return None
    return None


# --- Сопоставление по основам --------------------------------------------


def _has_stem(word: str, stems: frozenset[str]) -> bool:
    """Проверяет, начинается ли слово с одной из заданных основ (см. докстринг модуля)."""
    return any(word.startswith(stem) for stem in stems)


def _contains_stem(words: list[str], stems: frozenset[str]) -> bool:
    """Проверяет, есть ли среди слов фразы хоть одно, совпадающее с одной из основ."""
    return any(_has_stem(word, stems) for word in words)


# --- Числительные для команды volume -------------------------------------

# Явный словарь форм числительных 0..100, нужных для «громкость пятьдесят»,
# «на пятидесяти», «до сорока» и т. п. (см. докстринг модуля про то, почему
# здесь не подходит обрезанная основа). Формы взяты после нормализации «ё» → «е»
# (см. `_tokenize`), поэтому вариантов с «ё» тут нет — они не встретятся.
_NUMBER_WORDS: dict[str, int] = {
    "ноль": 0,
    "нуля": 0,
    "нулю": 0,
    "один": 1,
    "одна": 1,
    "одного": 1,
    "одной": 1,
    "два": 2,
    "двух": 2,
    "двум": 2,
    "три": 3,
    "трех": 3,
    "трем": 3,
    "четыре": 4,
    "четырех": 4,
    "четырем": 4,
    "пять": 5,
    "пяти": 5,
    "шесть": 6,
    "шести": 6,
    "семь": 7,
    "семи": 7,
    "восемь": 8,
    "восьми": 8,
    "девять": 9,
    "девяти": 9,
    "десять": 10,
    "десяти": 10,
    "одиннадцать": 11,
    "одиннадцати": 11,
    "двенадцать": 12,
    "двенадцати": 12,
    "тринадцать": 13,
    "тринадцати": 13,
    "четырнадцать": 14,
    "четырнадцати": 14,
    "пятнадцать": 15,
    "пятнадцати": 15,
    "шестнадцать": 16,
    "шестнадцати": 16,
    "семнадцать": 17,
    "семнадцати": 17,
    "восемнадцать": 18,
    "восемнадцати": 18,
    "девятнадцать": 19,
    "девятнадцати": 19,
    "двадцать": 20,
    "двадцати": 20,
    "тридцать": 30,
    "тридцати": 30,
    "сорок": 40,
    "сорока": 40,
    "пятьдесят": 50,
    "пятидесяти": 50,
    "шестьдесят": 60,
    "шестидесяти": 60,
    "семьдесят": 70,
    "семидесяти": 70,
    "восемьдесят": 80,
    "восьмидесяти": 80,
    "девяносто": 90,
    "девяноста": 90,
    "сто": 100,
    "ста": 100,
}

# Круглые десятки, после которых следующее число 1..9 образует составное
# числительное («сорок» + «пять» = 45), см. `_parse_number`.
_TENS_VALUES = frozenset({20, 30, 40, 50, 60, 70, 80, 90})


def _number_value(word: str) -> int | None:
    """Возвращает числовое значение слова — по словарю форм либо как цифры («50»)."""
    value = _NUMBER_WORDS.get(word)
    if value is not None:
        return value
    return int(word) if word.isdigit() else None


def _parse_number(words: list[str]) -> int | None:
    """Ищет в словах фразы число 0..100, включая составные вида «сорок пять».

    Берётся первое найденное числительное; если сразу за круглым десятком
    («сорок», «девяносто» и т. п.) идёт число 1..9, оба складываются в одно
    составное значение («сорок пять» → 45). Результат всегда ограничен
    диапазоном 0..100, как того требует громкость.
    """
    for index, word in enumerate(words):
        value = _number_value(word)
        if value is None:
            continue
        if value in _TENS_VALUES and index + 1 < len(words):
            next_value = _number_value(words[index + 1])
            if next_value is not None and 1 <= next_value <= 9:
                value += next_value
        return min(max(value, 0), 100)
    return None


# --- Правила разбора команд -----------------------------------------------
# Правила проверяются по порядку, от более специфичных к более общим —
# первое подошедшее выигрывает (см. `_RULES` и докстринг `parse_voice_command`).

_NOW_PLAYING_QUESTION_WORDS = frozenset({"что", "кто"})
_NOW_PLAYING_STEMS = frozenset({"игра", "звуч", "поет", "поют", "песн", "трек"})


def _rule_now_playing(words: list[str]) -> VoiceCommand | None:
    """«что играет», «что сейчас играет», «что это за песня», «кто поёт»."""
    if not any(word in _NOW_PLAYING_QUESTION_WORDS for word in words):
        return None
    if not _contains_stem(words, _NOW_PLAYING_STEMS):
        return None
    return VoiceCommand(action="now_playing")


_VOLUME_ABSOLUTE_STEMS = frozenset({"громкост", "звук"})
_VOLUME_UP_STEMS = frozenset({"громч", "погромч"})
_VOLUME_DOWN_STEMS = frozenset({"тиш", "потиш"})


def _rule_volume(words: list[str]) -> VoiceCommand | None:
    """Абсолютная («громкость пятьдесят») либо относительная («громче»/«тише») громкость."""
    if _contains_stem(words, _VOLUME_ABSOLUTE_STEMS):
        number = _parse_number(words)
        if number is not None:
            return VoiceCommand(action="volume", volume_percent=number)
    if _contains_stem(words, _VOLUME_UP_STEMS):
        return VoiceCommand(action="volume", volume_delta=20)
    if _contains_stem(words, _VOLUME_DOWN_STEMS):
        return VoiceCommand(action="volume", volume_delta=-20)
    return None


_RESUME_STEMS = frozenset({"продолж", "возобнов"})
_RESUME_PLAY_STEM = frozenset({"игра"})
_RESUME_FURTHER_STEM = frozenset({"дальш"})


def _rule_resume(words: list[str]) -> VoiceCommand | None:
    """«продолжай», «возобнови», «играй дальше»/«дальше играй» (порядок слов не важен).

    Проверяется раньше `_rule_skip`: у «дальше» самого по себе основа
    «дальш» совпадает и с `skip` («дальше» без «играй» — это следующий
    трек), поэтому только сочетание «игра»+«дальш» отличает «играй дальше»
    (продолжить) от голого «дальше» (переключить), см. докстринг модуля.
    """
    if _contains_stem(words, _RESUME_STEMS):
        return VoiceCommand(action="resume")
    if _contains_stem(words, _RESUME_PLAY_STEM) and _contains_stem(words, _RESUME_FURTHER_STEM):
        return VoiceCommand(action="resume")
    return None


_SKIP_STEMS = frozenset({"следующ", "дальш", "переключ", "пропуст", "скип", "друг"})


def _rule_skip(words: list[str]) -> VoiceCommand | None:
    """«следующий трек», «дальше», «переключи», «пропусти», «скип», «другую песню»."""
    return VoiceCommand(action="skip") if _contains_stem(words, _SKIP_STEMS) else None


_STOP_STEMS = frozenset({"отключ", "выключ", "уйд", "выйд", "хват", "закончи", "покинь"})


def _rule_stop(words: list[str]) -> VoiceCommand | None:
    """«отключись», «выключись», «уйди», «выйди», «хватит», «закончили», «покинь канал»."""
    return VoiceCommand(action="stop") if _contains_stem(words, _STOP_STEMS) else None


_PAUSE_STEMS = frozenset({"пауз", "останов", "стоп", "замолч"})


def _rule_pause(words: list[str]) -> VoiceCommand | None:
    """«пауза», «на паузу», «останови», голое «стоп», «замолчи».

    Проверяется после `_rule_stop`: «стоп отключись» уже ушло в `stop`
    двумя строками выше по основе «отключ», сюда доходит только голый
    «стоп» без слов про отключение — и тогда это пауза, а не отключение.
    """
    return VoiceCommand(action="pause") if _contains_stem(words, _PAUSE_STEMS) else None


_WAVE_STEM = "волн"
_WAVE_FROM_TRACK_MARKERS = frozenset({"от", "по"})


def _rule_wave(words: list[str]) -> VoiceCommand | None:
    """«включи мою волну», «моя волна» — просто волна; «волну от/по ...» — волна от трека."""
    for index, word in enumerate(words):
        if not word.startswith(_WAVE_STEM):
            continue
        rest = words[index + 1 :]
        if rest and rest[0] in _WAVE_FROM_TRACK_MARKERS and len(rest) > 1:
            return VoiceCommand(action="wave", query=" ".join(rest[1:]))
        return VoiceCommand(action="wave")
    return None


_SEARCH_COMMAND_STEMS = frozenset({"включ", "поставь", "найд", "сыграй", "запуст"})
_SEARCH_STOPWORDS = frozenset({"песню", "песня", "трек", "мне", "пожалуйста", "давай"})


def _rule_search(words: list[str]) -> VoiceCommand | None:
    """«включи»/«поставь»/«найди»/«сыграй»/«запусти» + остаток фразы как поисковый запрос.

    Самое общее правило, поэтому проверяется последним. Служебные слова
    («песню», «мне», «пожалуйста» и т. п.) выбрасываются из запроса вместе
    с самим командным словом; если после этого запрос пуст (голое «катя
    включи»), команда не распознана — возвращается `None`, а не поиск с
    пустой строкой.
    """
    command_index = None
    for index, word in enumerate(words):
        if _has_stem(word, _SEARCH_COMMAND_STEMS):
            command_index = index
            break
    if command_index is None:
        return None
    query_words = [
        word
        for index, word in enumerate(words)
        if index != command_index and word not in _SEARCH_STOPWORDS
    ]
    if not query_words:
        return None
    return VoiceCommand(action="search", query=" ".join(query_words))


# Порядок — часть контракта: правила идут от специфичных к общим, первое
# подошедшее выигрывает (см. пункты 1-8 задачи и докстринги отдельных правил
# про конфликты между resume/skip и stop/pause).
_RULES: tuple[Callable[[list[str]], VoiceCommand | None], ...] = (
    _rule_now_playing,
    _rule_volume,
    _rule_resume,
    _rule_skip,
    _rule_stop,
    _rule_pause,
    _rule_wave,
    _rule_search,
)


def parse_voice_command(text: str) -> VoiceCommand | None:
    """Разбирает текст, распознанный Vosk, в команду боту либо возвращает `None`.

    `None` означает одно из двух: во фразе нет обращения по имени бота (см.
    `WAKE_WORD` и `_extract_command_body`), либо обращение есть, но ни одно
    из правил в `_RULES` не подошло. Функция гарантированно не бросает
    исключений ни на каком входе — при любых сомнениях (пустая строка,
    мусор, неожиданный формат) результат просто `None`, широкий `except`
    внизу — намеренная защита именно этого контракта, а не небрежность.
    """
    try:
        words = _tokenize(text)
        body = _extract_command_body(words)
        if body is None:
            return None
        return _match_rules(body)
    except Exception:
        return None


def _match_rules(body: list[str]) -> VoiceCommand | None:
    """Прогоняет тело команды по правилам `_RULES`; первое подошедшее выигрывает."""
    for rule in _RULES:
        command = rule(body)
        if command is not None:
            return command
    return None


def is_wake_word_only(text: str) -> bool:
    """Признак того, что человек только окликнул бота по имени, но команду ещё не назвал.

    Vosk считает фразу законченной по паузе в речи, а обращение «Катя, …»
    люди произносят именно с паузой после имени — поэтому «Катя, следующий»
    сплошь и рядом приезжает сюда двумя отдельными фразами: сначала «катя»,
    затем «следующее». Такой оклик сам по себе командой не является, но
    означает, что следующую фразу этого же человека нужно принять как
    команду уже без повторного имени (см. `parse_command_body` и окно
    ожидания в `bot.cogs.voice_control`).
    """
    try:
        words = _tokenize(text)
        body = _extract_command_body(words)
        return body is not None and not body
    except Exception:
        return False


def parse_command_body(text: str) -> VoiceCommand | None:
    """Разбирает фразу как команду БЕЗ обращения по имени — продолжение после оклика.

    Вызывать можно только для фразы, пришедшей сразу за окликом по имени
    (см. `is_wake_word_only`) и от того же самого человека: без этого
    ограничения бот выполнял бы команды из любого разговора в канале, ни к
    нему не обращённого, — ровно то, от чего защищает требование имени в
    `parse_voice_command`.
    """
    try:
        words = _tokenize(text)
        if not words:
            return None
        return _match_rules(words)
    except Exception:
        return None
