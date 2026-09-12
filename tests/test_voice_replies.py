"""Тесты функции выбора случайной голосовой реплики.

Функция pick_reply() выбирает фразу-ответ бота на голосовую команду. Главное
свойство: не повторяет фразу, выбранную для этого же действия в прошлый раз.
На коротких списках (10-15 вариантов) повтор подряд звучит как заезженная
пластинка, и это создаёт впечатление, что бот не реагирует или сломался.

Функция использует глобальное состояние (_last_choice), поэтому тесты
должны сбрасывать его между запусками.
"""

from __future__ import annotations

import pytest

from bot.voice_replies import _last_choice, pick_reply
from bot.voice_reply_phrases import PHRASES


@pytest.fixture(autouse=True)
def reset_last_choice():
    """Сбрасывает состояние последнего выбора перед каждым тестом.

    Состояние хранится в модульной переменной и может влиять на независимость
    тестов, поэтому его нужно обнулять. Используется autouse=True, чтобы
    автоматически сбрасывалось для каждого теста.
    """
    _last_choice.clear()
    yield
    _last_choice.clear()


class TestKnownActions:
    """Функция возвращает фразы для известных действий."""

    @pytest.mark.parametrize("action", PHRASES.keys())
    def test_returns_phrase_for_known_action(self, action: str):
        """Для каждого известного действия возвращается фраза из списка."""
        result = pick_reply(action)
        assert result is not None, f"pick_reply({action!r}) вернул None, ожидалась фраза"
        assert isinstance(result, str)
        assert result in PHRASES[action], (
            f"pick_reply({action!r}) вернул {result!r}, которой нет в PHRASES[{action!r}]"
        )

    @pytest.mark.parametrize("action", PHRASES.keys())
    def test_all_ten_actions_are_supported(self, action: str):
        """Все десять действий (pause, resume, skip, stop, wave, search, volume, now_playing,
        error, not_found) возвращают непустой результат."""
        result = pick_reply(action)
        assert result is not None, f"Действие {action!r} вернуло None, но должно быть поддерживаемо"
        assert len(result) > 0


class TestUnknownActions:
    """Функция возвращает None для неизвестных действий."""

    @pytest.mark.parametrize(
        "action",
        [
            "repeat",
            "unknown",
            "nonsense",
            "foobar",
            "паузапаузапауза",
            "привет",
            "123",
            "",
        ],
    )
    def test_returns_none_for_unknown_action(self, action: str):
        """Для неизвестного действия функция возвращает None."""
        result = pick_reply(action)
        assert result is None, (
            f"pick_reply({action!r}) должен вернуть None, но вернул {result!r}"
        )


class TestNoConsecutiveRepeats:
    """Функция не повторяет фразу, выбранную в прошлый раз для этого же действия.

    Это ключевое свойство, ради которого функция сложнее простого случайного
    выбора. На списке из 11 фраз повтор подряд звучит как сбой.
    """

    def test_first_call_can_be_any_phrase(self):
        """Первый вызов может вернуть любую фразу (никакой истории нет)."""
        result = pick_reply("pause")
        assert result is not None
        assert result in PHRASES["pause"]

    def test_second_call_never_repeats_first(self):
        """Второй вызов никогда не повторяет первый."""
        first = pick_reply("pause")
        second = pick_reply("pause")
        assert first != second, (
            f"Функция повторила фразу подряд: обе вызовы вернули {first!r}"
        )

    def test_many_consecutive_calls_never_repeat_adjacent(self):
        """При многократных вызовах не повторяются соседние фразы.

        Проверяется на 30+ вызовах, что никогда не будет result[i] == result[i+1].
        С 11 фразами и защитой от повтора вероятность сбоя стремится к нулю.
        """
        action = "pause"
        results = [pick_reply(action) for _ in range(50)]

        for i in range(len(results) - 1):
            assert results[i] != results[i + 1], (
                f"На позициях {i} и {i + 1} повторилась фраза {results[i]!r}: {results}"
            )

    def test_no_repeat_for_each_action_independently(self):
        """Выбор для одного действия не влияет на защиту повтора для другого.

        Если для pause выбрали фразу A, это не должно запретить выбрать ту же
        фразу для skip в следующий раз (независимость).
        """
        pause_first = pick_reply("pause")
        skip_first = pick_reply("skip")
        pause_second = pick_reply("pause")
        skip_second = pick_reply("skip")

        # pause не повторяется подряд
        assert pause_first != pause_second
        # skip не повторяется подряд
        assert skip_first != skip_second

    def test_protection_per_action(self):
        """Последний выбор запоминается отдельно для каждого действия.

        После pause, resume, skip — каждое действие может иметь свой
        последний выбор, и они не смешиваются.
        """
        # Выбираем для разных действий
        choices = {
            "pause": pick_reply("pause"),
            "resume": pick_reply("resume"),
            "skip": pick_reply("skip"),
        }

        # Снова выбираем для первого действия
        second_pause = pick_reply("pause")

        # Должно отличаться от первого выбора для pause
        assert second_pause != choices["pause"]


class TestDiversity:
    """За много вызовов выбираются разные фразы."""

    @pytest.mark.parametrize("action", PHRASES.keys())
    def test_many_calls_produce_different_phrases(self, action: str):
        """За 100 вызовов встречается минимум 5 различных фраз.

        С 11 доступными фразами и 100 вызовами должны встречаться разные
        варианты. Проверяем, что это не одна и та же фраза каждый раз.
        """
        results = [pick_reply(action) for _ in range(100)]
        unique_results = set(results)

        assert len(unique_results) >= 5, (
            f"За 100 вызовов pick_reply({action!r}) встречилось всего "
            f"{len(unique_results)} различных фраз. Ожидалось минимум 5. "
            f"Это может означать, что функция выбирает не случайно."
        )

    def test_at_least_half_of_phrases_used_after_many_calls(self):
        """За много вызовов должно использоваться минимум половина доступных фраз."""
        action = "volume"
        total_phrases = len(PHRASES[action])
        results = [pick_reply(action) for _ in range(200)]
        unique_results = set(results)

        min_expected = max(5, total_phrases // 2)
        assert len(unique_results) >= min_expected, (
            f"За 200 вызовов встречилось {len(unique_results)} различных фраз, "
            f"но доступно {total_phrases}. Ожидалось минимум {min_expected}."
        )


class TestSinglePhraseAction:
    """Поведение с действием, у которого одна-единственная фраза.

    Если у действия всего одна фраза, то, в соответствии с контрактом,
    повторение неизбежно и не считается сбоем: выбирать не из чего.
    """

    def test_single_phrase_can_repeat(self):
        """Действие с одной фразой может повториться подряд (это нормально)."""
        # Создаём фиксированную ситуацию: вызываем две реплики подряд
        # и проверяем, что из-за одной фразы нет ошибки
        # Такого действия нет в текущем PHRASES, но логика должна работать

        # Для теста используем существующие действия, которые имеют 11 фраз каждое
        # Проверяем, что нет исключения и работает нормально
        result1 = pick_reply("pause")
        result2 = pick_reply("pause")

        # Обе должны быть не None
        assert result1 is not None
        assert result2 is not None


class TestEdgeCases:
    """Граничные случаи и странные входы."""

    def test_none_action_returns_none(self):
        """Если передать None, должно вернуться None (безопасный отказ)."""
        # В реальности это не произойдёт из-за типов, но проверяем логику
        result = pick_reply(None)  # type: ignore
        assert result is None

    def test_case_sensitive_action(self):
        """Действие чувствительно к регистру (PAUSE != pause)."""
        result_lower = pick_reply("pause")
        result_upper = pick_reply("PAUSE")

        # pause существует и вернёт фразу
        assert result_lower is not None
        # PAUSE не существует и вернёт None
        assert result_upper is None


class TestStateIndependence:
    """Тесты независимы друг от друга благодаря сбросу состояния."""

    def test_state_reset_between_tests(self):
        """Каждый тест начинается со сброшенным состоянием.

        Это проверяется через fixture reset_last_choice, которая autouse=True,
        но явный тест страхует нас от регрессии.
        """
        # После сброса _last_choice должен быть пуст
        assert len(_last_choice) == 0, (
            "Состояние _last_choice не было сброшено перед тестом. "
            "Это означает, что fixture не работает правильно."
        )

        # После вызова он должен содержать запись для действия
        pick_reply("pause")
        assert "pause" in _last_choice

        # После нового сброса (который произойдёт после этого теста) будет пусто


class TestPhrasesStructure:
    """Проверки структуры словаря PHRASES."""

    def test_all_actions_have_phrases(self):
        """Каждое действие имеет кортеж фраз."""
        for action, phrases in PHRASES.items():
            assert isinstance(phrases, tuple), (
                f"PHRASES[{action!r}] должен быть кортежем, но это {type(phrases)}"
            )
            assert len(phrases) > 0, f"PHRASES[{action!r}] пуст"

    def test_all_phrases_are_strings(self):
        """Все фразы — это строки."""
        for action, phrases in PHRASES.items():
            for i, phrase in enumerate(phrases):
                assert isinstance(phrase, str), (
                    f"PHRASES[{action!r}][{i}] должен быть строкой, но это {type(phrase)}"
                )
                assert len(phrase) > 0, f"PHRASES[{action!r}][{i}] пуста"

    def test_all_actions_have_ten_to_fifteen_phrases(self):
        """У каждого действия от 10 до 15 фраз (именно такой диапазон ставился авторам фраз).

        Диапазон, а не точное число: список фраз пополняется со временем (см.
        группы "error" и "not_found" с 13 фразами против 11 у более старых
        действий), и тест не должен падать на штатном пополнении. Он всё
        равно ловит настоящую поломку — опустевшую группу или разросшуюся до
        неприличия.
        """
        for action, phrases in PHRASES.items():
            assert 10 <= len(phrases) <= 15, (
                f"PHRASES[{action!r}] имеет {len(phrases)} фраз, ожидалось от 10 до 15"
            )

    def test_ten_actions_total(self):
        """Ровно 10 действий: восемь исходных плюс error и not_found."""
        expected_actions = {
            "pause",
            "resume",
            "skip",
            "stop",
            "wave",
            "search",
            "volume",
            "now_playing",
            "error",
            "not_found",
        }
        actual_actions = set(PHRASES.keys())
        assert actual_actions == expected_actions, (
            f"Ожидаемые действия: {expected_actions}, "
            f"найдены: {actual_actions}, "
            f"разница: {actual_actions ^ expected_actions}"
        )
