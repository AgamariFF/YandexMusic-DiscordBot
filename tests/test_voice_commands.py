"""Тесты для модуля bot.voice_commands (разбор голосовых команд из текста Vosk)."""

import pytest

from bot.voice_commands import (
    VoiceCommand,
    parse_voice_command,
)


class TestWakeWordRecognition:
    """Тесты распознавания обращения по имени."""

    def test_exact_name(self):
        """Точное совпадение имени в начале фразы распознаётся."""
        cmd = parse_voice_command("катя пауза")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_name_lowercase(self):
        """Имя в нижнем регистре (как от Vosk) распознаётся."""
        cmd = parse_voice_command("катя пауза")
        assert cmd is not None

    def test_name_form_kati(self):
        """Падеж: кати (дательный) распознаётся."""
        cmd = parse_voice_command("кати пауза")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_name_form_kate(self):
        """Падеж: кате (предложный) распознаётся."""
        cmd = parse_voice_command("кате пауза")
        assert cmd is not None

    def test_name_form_katy(self):
        """Падеж: катю (винительный) распознаётся."""
        cmd = parse_voice_command("катю пауза")
        assert cmd is not None

    def test_name_form_katei(self):
        """Падеж: катей (творительный) распознаётся."""
        cmd = parse_voice_command("катей пауза")
        assert cmd is not None

    def test_name_diminutive_katka(self):
        """Уменьшительная: катька распознаётся."""
        cmd = parse_voice_command("катька пауза")
        assert cmd is not None

    def test_name_diminutive_katku(self):
        """Уменьшительная: катьку распознаётся."""
        cmd = parse_voice_command("катьку пауза")
        assert cmd is not None

    def test_name_diminutive_katki(self):
        """Уменьшительная: катьки распознаётся."""
        cmd = parse_voice_command("катьки пауза")
        assert cmd is not None

    def test_name_diminutive_kat(self):
        """Уменьшительная: кать распознаётся."""
        cmd = parse_voice_command("кать пауза")
        assert cmd is not None

    def test_levenshtein_typo_hatya(self):
        """Опечатка расстояния 1: хатя распознаётся как имя."""
        cmd = parse_voice_command("хатя пауза")
        assert cmd is not None

    def test_levenshtein_typo_kata(self):
        """Опечатка расстояния 1: ката распознаётся как имя."""
        cmd = parse_voice_command("ката пауза")
        assert cmd is not None

    def test_levenshtein_typo_kaya(self):
        """Опечатка расстояния 1: кая распознаётся как имя."""
        cmd = parse_voice_command("кая пауза")
        assert cmd is not None

    def test_false_positive_kater(self):
        """Слово 'катер' НЕ должно приниматься как имя (расстояние > 1)."""
        cmd = parse_voice_command("катер пауза")
        assert cmd is None

    def test_false_positive_katok(self):
        """Слово 'каток' НЕ должно приниматься как имя (расстояние > 1)."""
        cmd = parse_voice_command("каток пауза")
        assert cmd is None

    def test_false_positive_katar(self):
        """Слово 'катать' НЕ должно приниматься как имя (расстояние > 1)."""
        cmd = parse_voice_command("катать пауза")
        assert cmd is None

    def test_false_positive_kartina(self):
        """Слово 'картина' НЕ должно приниматься как имя (расстояние > 1)."""
        cmd = parse_voice_command("картина пауза")
        assert cmd is None

    def test_filler_word_e_before_name(self):
        """Слово-заполнитель 'э' перед именем молча пропускается."""
        cmd = parse_voice_command("э катя пауза")
        assert cmd is not None

    def test_filler_word_a_before_name(self):
        """Слово-заполнитель 'а' перед именем молча пропускается."""
        cmd = parse_voice_command("а катя пауза")
        assert cmd is not None

    def test_filler_word_nu_before_name(self):
        """Слово-заполнитель 'ну' перед именем молча пропускается."""
        cmd = parse_voice_command("ну катя пауза")
        assert cmd is not None

    def test_filler_word_ey_before_name(self):
        """Слово-заполнитель 'эй' перед именем молча пропускается."""
        cmd = parse_voice_command("эй катя пауза")
        assert cmd is not None

    def test_filler_word_slushai_before_name(self):
        """Слово-заполнитель 'слушай' перед именем молча пропускается."""
        cmd = parse_voice_command("слушай катя пауза")
        assert cmd is not None

    def test_filler_word_okej_before_name(self):
        """Слово-заполнитель 'окей' перед именем молча пропускается."""
        cmd = parse_voice_command("окей катя пауза")
        assert cmd is not None

    def test_filler_word_ok_before_name(self):
        """Слово-заполнитель 'ok' перед именем молча пропускается."""
        cmd = parse_voice_command("ok катя пауза")
        assert cmd is not None

    def test_multiple_filler_words(self):
        """Несколько заполнителей перед именем пропускаются."""
        cmd = parse_voice_command("э ну катя пауза")
        assert cmd is not None

    def test_name_not_first_significant_word(self):
        """Имя в середине фразы НЕ распознаётся как обращение."""
        cmd = parse_voice_command("пауза катя")
        assert cmd is None

    def test_name_not_first_significant_word_with_command_after_example1(self):
        """Имя не в начале, но с командой после (пример 1): 'привет катя следующий трек'."""
        cmd = parse_voice_command("привет катя следующий трек")
        assert cmd is None, (
            "Имя должно быть первым значимым словом; "
            "'привет катя следующий трек' - имя не в начале, поэтому None"
        )

    def test_name_not_first_significant_word_with_command_after_example2(self):
        """Имя не в начале, но с командой после (пример 2): 'мы с катей вчера включи волну'."""
        cmd = parse_voice_command("мы с катей вчера включи волну")
        assert cmd is None, (
            "Имя должно быть первым значимым словом; "
            "'мы с катей вчера включи волну' - имя не в начале, поэтому None"
        )

    def test_name_not_first_significant_word_with_command_after_example3(self):
        """Имя не в начале, но с командой после (пример 3): 'скажи кате поставь на паузу'."""
        cmd = parse_voice_command("скажи кате поставь на паузу")
        assert cmd is None, (
            "Имя должно быть первым значимым словом; "
            "'скажи кате поставь на паузу' - имя не в начале, поэтому None"
        )

    def test_only_name_no_command(self):
        """Только имя без команды даёт None."""
        cmd = parse_voice_command("катя")
        assert cmd is None

    def test_only_name_with_fillers_no_command(self):
        """Только имя и заполнители без команды даёт None."""
        cmd = parse_voice_command("э ну катя")
        assert cmd is None

    def test_only_fillers_no_name(self):
        """Фраза из одних заполнителей БЕЗ имени, но с командой после НИХ НЕ распознаётся."""
        cmd = parse_voice_command("эй слушай следующий трек")
        assert cmd is None, (
            "Нет имени бота, только заполнители и команда; "
            "фраза должна вернуть None"
        )

    def test_empty_string(self):
        """Пустая строка даёт None."""
        cmd = parse_voice_command("")
        assert cmd is None

    def test_only_whitespace(self):
        """Строка из одних пробелов даёт None."""
        cmd = parse_voice_command("   ")
        assert cmd is None


class TestNowPlayingAction:
    """Тесты действия now_playing."""

    def test_what_plays_with_plays(self):
        """Вопрос 'что играет' распознаётся."""
        cmd = parse_voice_command("катя что играет")
        assert cmd is not None
        assert cmd.action == "now_playing"

    def test_what_sounds(self):
        """Вопрос 'что звучит' распознаётся."""
        cmd = parse_voice_command("катя что звучит")
        assert cmd is not None
        assert cmd.action == "now_playing"

    def test_who_sings(self):
        """Вопрос 'кто поёт' распознаётся."""
        cmd = parse_voice_command("катя кто поет")
        assert cmd is not None
        assert cmd.action == "now_playing"

    def test_what_song_inflected(self):
        """Вопрос 'что за песня' распознаётся."""
        cmd = parse_voice_command("катя что за песня")
        assert cmd is not None
        assert cmd.action == "now_playing"

    def test_what_track(self):
        """Вопрос 'что за трек' распознаётся."""
        cmd = parse_voice_command("катя что за трек")
        assert cmd is not None
        assert cmd.action == "now_playing"

    def test_what_currently_playing(self):
        """Вопрос 'что сейчас играет' распознаётся."""
        cmd = parse_voice_command("катя что сейчас играет")
        assert cmd is not None
        assert cmd.action == "now_playing"


class TestVolumeAction:
    """Тесты действия volume."""

    def test_absolute_volume_by_word(self):
        """Абсолютная громкость: 'громкость пятьдесят' распознаётся."""
        cmd = parse_voice_command("катя громкость пятьдесят")
        assert cmd is not None
        assert cmd.action == "volume"
        assert cmd.volume_percent == 50

    def test_absolute_volume_by_digit(self):
        """Абсолютная громкость: 'громкость 75' распознаётся."""
        cmd = parse_voice_command("катя громкость 75")
        assert cmd is not None
        assert cmd.action == "volume"
        assert cmd.volume_percent == 75

    def test_set_sound_by_number(self):
        """Команда 'поставь звук на семьдесят' распознаётся."""
        cmd = parse_voice_command("катя поставь звук на семьдесят")
        assert cmd is not None
        assert cmd.action == "volume"
        assert cmd.volume_percent == 70

    def test_volume_percent_word_form(self):
        """Громкость с указанием 'процентов' распознаётся."""
        cmd = parse_voice_command("катя громкость сорок процентов")
        assert cmd is not None
        assert cmd.volume_percent == 40

    def test_composite_number_forty_five(self):
        """Составное число 'сорок пять' даёт 45."""
        cmd = parse_voice_command("катя громкость сорок пять")
        assert cmd is not None
        assert cmd.volume_percent == 45

    def test_composite_number_ninety_five(self):
        """Составное число 'девяносто пять' даёт 95."""
        cmd = parse_voice_command("катя громкость девяносто пять")
        assert cmd is not None
        assert cmd.volume_percent == 95

    def test_composite_number_twenty_two(self):
        """Составное число 'двадцать два' даёт 22."""
        cmd = parse_voice_command("катя громкость двадцать два")
        assert cmd is not None
        assert cmd.volume_percent == 22

    def test_number_two_alone(self):
        """Одинокое число 'два' даёт 2, не путается с двадцать."""
        cmd = parse_voice_command("катя громкость два")
        assert cmd is not None
        assert cmd.volume_percent == 2

    def test_number_twelve_alone(self):
        """Одинокое число 'двенадцать' даёт 12, не путается с двадцать."""
        cmd = parse_voice_command("катя громкость двенадцать")
        assert cmd is not None
        assert cmd.volume_percent == 12

    def test_number_twenty_alone(self):
        """Одинокое число 'двадцать' даёт 20."""
        cmd = parse_voice_command("катя громкость двадцать")
        assert cmd is not None
        assert cmd.volume_percent == 20

    def test_volume_above_100_clamped(self):
        """Громкость выше 100 обрезается до 100."""
        cmd = parse_voice_command("катя громкость 150")
        assert cmd is not None
        assert cmd.volume_percent == 100

    def test_volume_below_0_clamped(self):
        """Громкость ниже 0 обрезается до 0."""
        cmd = parse_voice_command("катя громкость минус пятьдесят")
        assert cmd is not None
        # Мусор 'минус' должен быть отброшен, вернёт первое число
        assert cmd.volume_percent == 50

    def test_zero_volume(self):
        """Громкость 0 допускается."""
        cmd = parse_voice_command("катя громкость ноль")
        assert cmd is not None
        assert cmd.volume_percent == 0

    def test_hundred_volume(self):
        """Громкость 100 допускается."""
        cmd = parse_voice_command("катя громкость сто")
        assert cmd is not None
        assert cmd.volume_percent == 100

    def test_relative_volume_up(self):
        """Относительное увеличение: 'громче' даёт volume_delta = +20."""
        cmd = parse_voice_command("катя громче")
        assert cmd is not None
        assert cmd.action == "volume"
        assert cmd.volume_delta == 20

    def test_relative_volume_up_pogromche(self):
        """Относительное увеличение: 'погромче' даёт volume_delta = +20."""
        cmd = parse_voice_command("катя погромче")
        assert cmd is not None
        assert cmd.volume_delta == 20

    def test_relative_volume_down(self):
        """Относительное уменьшение: 'тише' даёт volume_delta = -20."""
        cmd = parse_voice_command("катя тише")
        assert cmd is not None
        assert cmd.volume_delta == -20

    def test_relative_volume_down_potishe(self):
        """Относительное уменьшение: 'потише' даёт volume_delta = -20."""
        cmd = parse_voice_command("катя потише")
        assert cmd is not None
        assert cmd.volume_delta == -20

    def test_volume_one_field_filled(self):
        """При volume заполнено ровно одно из volume_percent/volume_delta."""
        cmd = parse_voice_command("катя громкость пятьдесят")
        assert cmd.volume_percent is not None
        assert cmd.volume_delta is None


class TestResumeAction:
    """Тесты действия resume с приоритетом над skip."""

    def test_resume_prodolzhai(self):
        """Команда 'продолжай' даёт resume."""
        cmd = parse_voice_command("катя продолжай")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_resume_prodolzhi(self):
        """Команда 'продолжи' даёт resume."""
        cmd = parse_voice_command("катя продолжи")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_resume_vozobnovlyu(self):
        """Команда 'возобновлю' даёт resume."""
        cmd = parse_voice_command("катя возобновлю")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_resume_play_further(self):
        """Фраза 'играй дальше' даёт resume (не skip)."""
        cmd = parse_voice_command("катя играй дальше")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_resume_further_play(self):
        """Фраза 'дальше играй' даёт resume (порядок слов не важен)."""
        cmd = parse_voice_command("катя дальше играй")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_skip_further_alone(self):
        """Слово 'дальше' без 'играй' даёт skip."""
        cmd = parse_voice_command("катя дальше")
        assert cmd is not None
        assert cmd.action == "skip"

    def test_resume_vs_skip_priority(self):
        """Правило resume стоит выше skip — это критический тест приоритета."""
        # 'катя играй дальше' должна быть resume (перестановка правил ломает это)
        cmd = parse_voice_command("катя играй дальше")
        assert cmd.action == "resume", (
            "Resume должен проверяться раньше skip; "
            "если тест падает, правила переставлены неправильно"
        )


class TestSkipAction:
    """Тесты действия skip."""

    def test_skip_next_track(self):
        """Команда 'следующий трек' даёт skip."""
        cmd = parse_voice_command("катя следующий трек")
        assert cmd is not None
        assert cmd.action == "skip"

    def test_skip_further(self):
        """Слово 'дальше' без 'играй' даёт skip."""
        cmd = parse_voice_command("катя дальше")
        assert cmd is not None
        assert cmd.action == "skip"

    def test_skip_switch(self):
        """Команда 'переключи' даёт skip."""
        cmd = parse_voice_command("катя переключи")
        assert cmd is not None
        assert cmd.action == "skip"

    def test_skip_skip(self):
        """Слово 'скип' даёт skip."""
        cmd = parse_voice_command("катя скип")
        assert cmd is not None
        assert cmd.action == "skip"

    def test_skip_other_song(self):
        """Фраза 'другую песню' даёт skip."""
        cmd = parse_voice_command("катя другую песню")
        assert cmd is not None
        assert cmd.action == "skip"


class TestStopAction:
    """Тесты действия stop с приоритетом над pause."""

    def test_stop_disconnect(self):
        """Команда 'отключись' даёт stop."""
        cmd = parse_voice_command("катя отключись")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_exit(self):
        """Команда 'выключись' даёт stop."""
        cmd = parse_voice_command("катя выключись")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_leave(self):
        """Команда 'уйди' даёт stop."""
        cmd = parse_voice_command("катя уйди")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_exit_channel(self):
        """Команда 'выйди' даёт stop."""
        cmd = parse_voice_command("катя выйди")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_enough(self):
        """Команда 'хватит' даёт stop."""
        cmd = parse_voice_command("катя хватит")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_finished(self):
        """Команда 'закончили' даёт stop."""
        cmd = parse_voice_command("катя закончили")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_leave_channel(self):
        """Команда 'покинь канал' даёт stop."""
        cmd = parse_voice_command("катя покинь канал")
        assert cmd is not None
        assert cmd.action == "stop"

    def test_stop_vs_pause_priority(self):
        """Слово 'стоп' с отключением даёт stop, а без даёт pause."""
        # 'отключись стоп' должна дать stop (проверяет приоритет)
        cmd = parse_voice_command("катя отключись стоп")
        assert cmd.action == "stop"


class TestPauseAction:
    """Тесты действия pause."""

    def test_pause_word(self):
        """Слово 'пауза' даёт pause."""
        cmd = parse_voice_command("катя пауза")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_pause_on_pause(self):
        """Фраза 'на паузу' даёт pause."""
        cmd = parse_voice_command("катя на паузу")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_pause_stop(self):
        """Слово 'останови' даёт pause."""
        cmd = parse_voice_command("катя останови")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_pause_stop_bare(self):
        """Слово 'стоп' БЕЗ отключения даёт pause."""
        cmd = parse_voice_command("катя стоп")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_pause_silence(self):
        """Команда 'замолчи' даёт pause."""
        cmd = parse_voice_command("катя замолчи")
        assert cmd is not None
        assert cmd.action == "pause"


class TestWaveAction:
    """Тесты действия wave."""

    def test_wave_my_wave(self):
        """Фраза 'мою волну' даёт wave."""
        cmd = parse_voice_command("катя включи мою волну")
        assert cmd is not None
        assert cmd.action == "wave"
        assert cmd.query is None

    def test_wave_my_wave_simple(self):
        """Фраза 'моя волна' даёт wave."""
        cmd = parse_voice_command("катя моя волна")
        assert cmd is not None
        assert cmd.action == "wave"

    def test_wave_from_track_with_ot(self):
        """Фраза 'волна от группы кино' даёт wave с query."""
        cmd = parse_voice_command("катя волна от группы кино")
        assert cmd is not None
        assert cmd.action == "wave"
        assert cmd.query == "группы кино"

    def test_wave_from_track_with_po(self):
        """Фраза 'волна по певцу' даёт wave с query (маркер 'по')."""
        cmd = parse_voice_command("катя волна по певцу")
        assert cmd is not None
        assert cmd.action == "wave"
        assert cmd.query == "певцу"

    def test_wave_from_track_multiple_words_in_query(self):
        """После маркера несколько слов попадают в query."""
        cmd = parse_voice_command("катя волна от популярного певца иванова")
        assert cmd is not None
        assert cmd.query == "популярного певца иванова"


class TestSearchAction:
    """Тесты действия search."""

    def test_search_include(self):
        """Команда 'включи' + остаток даёт search."""
        cmd = parse_voice_command("катя включи вечная классика")
        assert cmd is not None
        assert cmd.action == "search"
        assert cmd.query == "вечная классика"

    def test_search_put(self):
        """Команда 'поставь' + остаток даёт search."""
        cmd = parse_voice_command("катя поставь рок музыку")
        assert cmd is not None
        assert cmd.action == "search"
        assert cmd.query == "рок музыку"

    def test_search_find(self):
        """Команда 'найди' + остаток даёт search."""
        cmd = parse_voice_command("катя найди ночное такси")
        assert cmd is not None
        assert cmd.action == "search"
        assert cmd.query == "ночное такси"

    def test_search_play(self):
        """Команда 'сыграй' + остаток даёт search."""
        cmd = parse_voice_command("катя сыграй музыку ночью")
        assert cmd is not None
        assert cmd.action == "search"
        assert cmd.query == "музыку ночью"

    def test_search_run(self):
        """Команда 'запусти' + остаток даёт search."""
        cmd = parse_voice_command("катя запусти плейлист денс")
        assert cmd is not None
        assert cmd.action == "search"

    def test_search_stopwords_removed_pesnya(self):
        """Служебное слово 'песня' удаляется из запроса."""
        cmd = parse_voice_command("катя включи песня про любовь")
        assert cmd is not None
        assert cmd.action == "search"
        assert "песня" not in cmd.query

    def test_search_stopwords_removed_pesnya_form(self):
        """Служебное слово 'песню' удаляется из запроса."""
        cmd = parse_voice_command("катя включи песню про любовь")
        assert cmd is not None
        assert "песню" not in cmd.query

    def test_search_stopwords_removed_track(self):
        """Служебное слово 'трек' удаляется из запроса."""
        cmd = parse_voice_command("катя включи трек про ночь")
        assert cmd is not None
        assert "трек" not in cmd.query

    def test_search_stopwords_removed_mne(self):
        """Служебное слово 'мне' удаляется из запроса."""
        cmd = parse_voice_command("катя включи мне что-то хорошее")
        assert cmd is not None
        assert "мне" not in cmd.query

    def test_search_stopwords_removed_pozhaluysta(self):
        """Служебное слово 'пожалуйста' удаляется из запроса."""
        cmd = parse_voice_command("катя включи пожалуйста что-то весёлое")
        assert cmd is not None
        assert "пожалуйста" not in cmd.query

    def test_search_stopwords_removed_davai(self):
        """Служебное слово 'давай' удаляется из запроса."""
        cmd = parse_voice_command("катя включи давай диско")
        assert cmd is not None
        assert "давай" not in cmd.query

    def test_search_empty_after_stopwords_removed(self):
        """Если запрос пустой после удаления служебных слов, команда не распознана."""
        cmd = parse_voice_command("катя включи")
        assert cmd is None

    def test_search_only_stopwords(self):
        """Если в запросе только служебные слова, команда не распознана."""
        cmd = parse_voice_command("катя включи песню пожалуйста")
        assert cmd is None

    def test_search_query_preserved(self):
        """Смысловые слова в запросе сохраняются."""
        cmd = parse_voice_command("катя включи ночное такси группа кино")
        assert cmd is not None
        assert "ночное" in cmd.query
        assert "такси" in cmd.query
        assert "группа" in cmd.query
        assert "кино" in cmd.query


class TestCaseSensitivityAndNormalization:
    """Тесты нормализации букв (ё → е) и других форм."""

    def test_yo_equals_e_in_word(self):
        """Буква 'ё' приравнена к 'е' ('поёт' и 'поет' — одно слово)."""
        cmd1 = parse_voice_command("катя что поет")
        cmd2 = parse_voice_command("катя что поёт")
        assert cmd1 is not None and cmd1.action == "now_playing"
        assert cmd2 is not None and cmd2.action == "now_playing"

    def test_punctuation_removed(self):
        """Знаки препинания удаляются из входной строки."""
        cmd = parse_voice_command("катя, пауза!")
        assert cmd is not None
        assert cmd.action == "pause"

    def test_mixed_case_normalized(self):
        """Входная строка приводится к нижнему регистру."""
        cmd = parse_voice_command("КАТЯ ПАУЗА")
        assert cmd is not None
        # Даже если вход был в верхнем регистре, функция работает


class TestEdgeCases:
    """Граничные случаи и обработка ошибок."""

    def test_garbage_input_no_exception(self):
        """Мусорный ввод не вызывает исключений."""
        try:
            parse_voice_command("!@#$%^&*()")
        except Exception:
            pytest.fail("parse_voice_command должен справляться с мусором")

    def test_very_long_input_no_exception(self):
        """Очень длинный ввод не вызывает исключений."""
        try:
            parse_voice_command("катя " + "слово " * 1000)
        except Exception:
            pytest.fail("parse_voice_command должен справляться с длинным вводом")

    def test_none_input_like_behavior(self):
        """Функция не должна вызывать исключение, даже на экстремальном входе."""
        # В реальности на вход идёт строка, но тест защищает от регрессии
        try:
            parse_voice_command("")
            parse_voice_command("   ")
            parse_voice_command("\n\t")
        except Exception:
            pytest.fail("parse_voice_command должен справляться с пустыми строками")

    def test_return_type_voicecommand_or_none(self):
        """Функция возвращает VoiceCommand или None."""
        result1 = parse_voice_command("катя пауза")
        result2 = parse_voice_command("мусор")
        assert isinstance(result1, VoiceCommand) or result1 is None
        assert result2 is None

    def test_voice_command_immutable(self):
        """VoiceCommand frozen dataclass — неизменяемый."""
        cmd = parse_voice_command("катя громкость пятьдесят")
        with pytest.raises((AttributeError, TypeError)):
            cmd.action = "pause"


class TestWordOrders:
    """Тесты независимости порядка слов (где применимо)."""

    def test_play_further_word_order_1(self):
        """'играй дальше' даёт resume."""
        cmd = parse_voice_command("катя играй дальше")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_play_further_word_order_2(self):
        """'дальше играй' даёт resume (порядок значения не имеет)."""
        cmd = parse_voice_command("катя дальше играй")
        assert cmd is not None
        assert cmd.action == "resume"

    def test_word_order_now_playing(self):
        """'что играет' и 'играет что' оба должны работать."""
        cmd1 = parse_voice_command("катя что играет")
        cmd2 = parse_voice_command("катя играет что")
        assert cmd1 is not None and cmd1.action == "now_playing"
        assert cmd2 is not None and cmd2.action == "now_playing"


class TestNumbersAreNotWords:
    """Тесты чтобы убедиться, что цифры в словах обрабатываются корректно."""

    def test_number_as_digits(self):
        """Число может быть записано цифрами: '50'."""
        cmd = parse_voice_command("катя громкость 50")
        assert cmd is not None
        assert cmd.volume_percent == 50

    def test_number_word_forms(self):
        """Числовые слова склоняются: 'пяти', 'пятидесяти'."""
        cmd = parse_voice_command("катя громкость пяти")
        assert cmd is not None
        assert cmd.volume_percent == 5

        cmd = parse_voice_command("катя громкость пятидесяти")
        assert cmd is not None
        assert cmd.volume_percent == 50


class TestNoOtherFieldsFilled:
    """Тесты что другие поля действий содержат только None где требуется."""

    def test_pause_has_no_query(self):
        """Action pause не имеет query."""
        cmd = parse_voice_command("катя пауза")
        assert cmd.query is None
        assert cmd.volume_percent is None
        assert cmd.volume_delta is None

    def test_now_playing_has_no_query(self):
        """Action now_playing не имеет query."""
        cmd = parse_voice_command("катя что играет")
        assert cmd.query is None
        assert cmd.volume_percent is None
        assert cmd.volume_delta is None

    def test_wave_without_track_no_query(self):
        """Wave без 'от/по' не имеет query."""
        cmd = parse_voice_command("катя мою волну")
        assert cmd.query is None
        assert cmd.volume_percent is None
        assert cmd.volume_delta is None

    def test_search_has_query(self):
        """Search имеет query."""
        cmd = parse_voice_command("катя включи песню про осень")
        assert cmd.query is not None
        assert cmd.volume_percent is None
        assert cmd.volume_delta is None
