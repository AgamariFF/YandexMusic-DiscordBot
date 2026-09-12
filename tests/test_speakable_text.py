"""Тесты функции приведения текста к озвучиваемому виду.

Функция speakable_text() преобразует текст так, чтобы его мог озвучить синтезатор речи:
цифры в русские слова, латиница в кириллицу по звучанию, посторонние символы удаляются.
Главное требование: результат НИКОГДА не содержит цифр, латинских букв и посторонних
символов, иначе синтезатор упадёт исключением.

Функция не бросает исключений ни на каком входе — от неё зависит любая произнесённая
ботом фраза, и одна неучтённая мелочь не должна ронять синтез целиком.
"""

from __future__ import annotations

import re

import pytest

from bot.tts import speakable_text

# Регулярное выражение, которому должен соответствовать любой результат функции.
# Только русские буквы (включая ё), пробелы, запятые и точки.
_SPEAKABLE_PATTERN = re.compile(r"^[А-Яа-яЁё ,.]*$")


def _is_speakable(text: str) -> bool:
    """Проверяет, что текст содержит только озвучиваемые символы."""
    return bool(_SPEAKABLE_PATTERN.match(text))


class TestInvariant:
    """Главное свойство: результат содержит только озвучиваемые символы.

    Это не украшение, а защита от падения синтезатора. Если функция вернёт
    хотя бы одну цифру, латинскую букву или посторонний символ, синтез упадёт
    исключением. Поэтому это должно быть самым надёжным тестом.
    """

    @pytest.mark.parametrize(
        "text",
        [
            # Русский текст
            "привет",
            "большой русский текст с пробелами и знаками, вроде точки. и запятой",
            # Цифры
            "в тексте есть число 50",
            "с датой 2024-09-12 и временем 14:30",
            "123",
            "0",
            "9999",
            # Латиница
            "hello world",
            "Nirvana",
            "ok",
            "MiXeD CaSe",
            "URL: https://example.com",
            # Символы
            "привет! как дела?",
            "текст—с—тире",
            'текст "в кавычках"',
            "текст (в скобках)",
            "проценты: 100%",
            "в цене: 50₽",
            # Эмодзи
            "привет 😀",
            "❤️ музыка",
            "🎵🎶🎤",
            # Смешанный контент
            "Nirvana - Come As You Are (1991) remix!",
            "hello мир 123",
            "123 abc привет",
            # Граничные случаи
            "",
            "   ",
            "\n\t",
            "!!!!!",
            "...........",
            "," * 10,
            # Очень длинные строки
            "а" * 1000,
            "привет " * 500,
            "a" * 500 + " привет",
        ],
    )
    def test_result_never_contains_non_speakable_chars(self, text: str):
        """Результат функции всегда содержит только озвучиваемые символы."""
        result = speakable_text(text)
        assert _is_speakable(result), (
            f"Результат {result!r} содержит недопустимые символы. "
            f"Должен соответствовать pattern: {_SPEAKABLE_PATTERN.pattern}"
        )


class TestNoExceptions:
    """Функция не бросает исключений ни на каком входе."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "нормальный текст",
            "123",
            "hello",
            "!!!@@@###$$$",
            "смешанный текст 123 hello",
            "null\x00byte",
            "\x01\x02\x03",
            "ሙስሊም",  # Амхарский
            "你好",  # Китайский
            "Привет\r\nтекст\tс\nпереводами",
            "   \n\t   ",
            "👍😊🎵" * 100,
        ],
    )
    def test_no_exception_on_any_input(self, text: str):
        """Функция не должна бросить исключение ни на каком входе."""
        try:
            speakable_text(text)
        except Exception as exc:
            pytest.fail(f"speakable_text() бросил {type(exc).__name__}: {exc}")


class TestNumbers:
    """Числа 0..100 превращаются в русские слова."""

    @pytest.mark.parametrize(
        "num,expected_word",
        [
            # Единицы
            (0, "ноль"),
            (1, "один"),
            (2, "два"),
            (3, "три"),
            (4, "четыре"),
            (5, "пять"),
            (6, "шесть"),
            (7, "семь"),
            (8, "восемь"),
            (9, "девять"),
            # Специальные 10-19
            (10, "десять"),
            (11, "одиннадцать"),
            (12, "двенадцать"),
            (13, "тринадцать"),
            (14, "четырнадцать"),
            (15, "пятнадцать"),
            (16, "шестнадцать"),
            (17, "семнадцать"),
            (18, "восемнадцать"),
            (19, "девятнадцать"),
            # Круглые десятки
            (20, "двадцать"),
            (30, "тридцать"),
            (40, "сорок"),
            (50, "пятьдесят"),
            (60, "шестьдесят"),
            (70, "семьдесят"),
            (80, "восемьдесят"),
            (90, "девяносто"),
            # Составные
            (21, "двадцать один"),
            (42, "сорок два"),
            (99, "девяносто девять"),
            # Сто
            (100, "сто"),
        ],
    )
    def test_numbers_0_to_100_as_words(self, num: int, expected_word: str):
        """Числа 0..100 озвучиваются как полные русские слова."""
        result = speakable_text(str(num))
        assert result == expected_word, (
            f"Число {num} должно быть озвучено как '{expected_word}', "
            f"но получилось '{result}'"
        )

    def test_number_in_text(self):
        """Число в контексте других слов озвучивается корректно."""
        result = speakable_text("громкость 50 процентов")
        assert "пятьдесят" in result
        assert "50" not in result

    def test_large_numbers_digit_by_digit(self):
        """Числа больше 100 озвучиваются поразрядно."""
        result = speakable_text("2024")
        # Большое число не должно быть озвучено, как 2024, а должно быть разобрано
        # на цифры (например "два ноль два четыре")
        assert "2024" not in result
        assert _is_speakable(result)

    def test_number_with_leading_zero(self):
        """Числа с ведущим нулём озвучиваются поразрядно."""
        result = speakable_text("007")
        # "007" должно быть "ноль ноль семь", а не "семь"
        assert "007" not in result
        assert _is_speakable(result)


class TestLatin:
    """Латиница транслитерируется в кириллицу по звучанию."""

    @pytest.mark.parametrize(
        "latin,contains_cyrillic",
        [
            ("hello", True),  # Не пустой результат
            ("ok", True),
            ("Nirvana", True),
            ("world", True),
            ("abc", True),
        ],
    )
    def test_latin_becomes_cyrillic(self, latin: str, contains_cyrillic: bool):
        """Латинские слова озвучиваются кириллицей."""
        result = speakable_text(latin)
        if contains_cyrillic:
            # Результат должен содержать только кириллицу/пробелы/пунктуацию
            assert _is_speakable(result)
            # И не должен содержать исходную латиницу
            assert not any(c.lower() in "abcdefghijklmnopqrstuvwxyz" for c in latin if c in result)

    def test_latin_in_text(self):
        """Латиница в контексте озвучивается."""
        result = speakable_text("Nirvana - Smells Like Teen Spirit")
        assert "Nirvana" not in result
        assert _is_speakable(result)

    def test_mixed_case_latin(self):
        """Латиница любого регистра озвучивается."""
        result1 = speakable_text("Hello")
        result2 = speakable_text("HELLO")
        result3 = speakable_text("hello")
        # Все должны дать озвучиваемый результат (какие-то из них могут совпадать)
        assert _is_speakable(result1)
        assert _is_speakable(result2)
        assert _is_speakable(result3)


class TestSymbolRemoval:
    """Посторонние символы удаляются."""

    @pytest.mark.parametrize(
        "symbol",
        [
            "!",
            "@",
            "#",
            "$",
            "%",
            "^",
            "&",
            "*",
            "(",
            ")",
            "-",
            "_",
            "+",
            "=",
            "[",
            "]",
            "{",
            "}",
            "|",
            "\\",
            ";",
            ":",
            "'",
            '"',
            "<",
            ">",
            "/",
            "?",
            "~",
            "`",
            "×",
            "÷",
            "€",
            "¥",
            "₽",
        ],
    )
    def test_special_chars_removed(self, symbol: str):
        """Посторонние символы удаляются."""
        result = speakable_text(f"привет{symbol}мир")
        assert symbol not in result


class TestWhitespaceCollapse:
    """Лишние пробелы схлопываются."""

    def test_extra_spaces_collapsed(self):
        """Множественные пробелы схлопываются в одиночные."""
        result = speakable_text("привет    мир")
        assert result == "привет мир"

    def test_leading_trailing_spaces_removed(self):
        """Пробелы в начале и конце удаляются."""
        result = speakable_text("  привет мир  ")
        assert result == "привет мир"

    def test_newlines_and_tabs_become_spaces(self):
        """Переводы строк и табуляции становятся пробелами, затем схлопываются."""
        result = speakable_text("привет\n\nмир\tпривет")
        # После обработки переводы строк/табуляции исчезают (становятся пробелами),
        # лишние пробелы схлопываются
        assert "\n" not in result
        assert "\t" not in result
        # Результат должен быть озвучиваем
        assert _is_speakable(result)


class TestEmptyResult:
    """Пустой результат для входа, где нечего озвучивать."""

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "\n",
            "\t",
            "\n\t  \n",
            "!!!",
            "???",
            "@@@",
            "---",
            "   !!!   ???   ",
        ],
    )
    def test_empty_text_returns_empty_string(self, text: str):
        """Текст, где нечего озвучивать, возвращает пустую строку."""
        result = speakable_text(text)
        assert result == "", f"Текст {text!r} должен дать пустую строку, но получился {result!r}"

    def test_punctuation_only_returns_punctuation(self):
        """Текст только из допустимых символов (точек/запятых) возвращает их как есть.

        Точки и запятые — допустимые символы для озвучивания (для пауз и интонации),
        поэтому они сохраняются в результате и схлопываются в пробелы при нужде.
        """
        result = speakable_text("...")
        # Точки допустимы и сохраняются
        assert _is_speakable(result)


class TestCombinedCases:
    """Комбинированные случаи из задачи."""

    def test_volume_example(self):
        """Пример из задачи: громкость с числом."""
        result = speakable_text("поставила громкость 50 процентов")
        assert "пятьдесят" in result
        assert "50" not in result
        assert _is_speakable(result)

    def test_nirvana_example(self):
        """Пример из задачи: название трека с тире."""
        result = speakable_text("Nirvana - Smells Like Teen Spirit")
        assert "-" not in result
        assert "Nirvana" not in result
        assert _is_speakable(result)

    def test_movie_title_with_em_dash(self):
        """Пример из задачи: длинное тире в названии."""
        result = speakable_text("Кино — Группа крови")
        assert "—" not in result
        assert result == "Кино Группа крови"

    def test_ok_word(self):
        """Пример из задачи: слово ok."""
        result = speakable_text("ok")
        assert result == "ок"
        assert _is_speakable(result)

    def test_hello_with_punctuation_and_percent(self):
        """Пример из задачи: привет с пунктуацией и процентами."""
        result = speakable_text("привет!!! 100%")
        assert result == "привет сто"
        assert _is_speakable(result)

    def test_only_question_marks(self):
        """Пример из задачи: только знаки препинания."""
        result = speakable_text("???")
        assert result == ""
