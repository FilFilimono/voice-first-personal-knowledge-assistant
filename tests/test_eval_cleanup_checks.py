"""Проверки замера чистки: что код обязан ловить, а что — не обязан.

Проверяется **не модель, а инструмент, которым её меряют.** Живой вызов идёт
в `just eval-cleanup` и делается владельцем: из сессии агента провайдер
недоступен (гео-блокировка). Здесь только чистые функции сравнения.

Тексты придуманы **по форме**, а не взяты из архива: тесту важна форма фразы
(«потерялось число», «появилось слово»), а не то, что владелец сказал на самом
деле. Так это и разрешено ADR-0002, дополнением от 19 августа.

Отдельная забота файла — **имена собственные в русском тексте**. Расшифровка
ставит заглавные как придётся, а чистка обязана их расставить: правило «слово
с заглавной — имя собственное» дало бы ложное срабатывание на каждой первой
букве предложения. Тесты ниже фиксируют, как это разведено.
"""

import pytest

from evals.cleanup import checks


def test_catches_invented_word() -> None:
    """Слово, которого не было в дословном варианте, — выдумка."""
    report = checks.compare("надо посмотреть фильм", "Надо обязательно посмотреть фильм.")

    assert "обязательно" in report.new_words
    assert not report.ok


def test_catches_changed_number() -> None:
    report = checks.compare("напомни в 19:40", "Напомни в 19:30.")

    assert report.lost_numbers == ["19:40"]
    assert not report.ok


def test_catches_dropped_negation() -> None:
    """Потерянное «не» переворачивает запись целиком."""
    report = checks.compare("решил не браться за это", "Решил браться за это.")

    assert report.lost_negations == 1
    assert not report.ok


def test_allows_removing_filler() -> None:
    report = checks.compare("э ну надо мм посмотреть фильм", "Надо посмотреть фильм.")

    assert report.ok
    assert report.new_words == []


def test_punctuation_and_capitals_are_not_changes() -> None:
    """Расстановка знаков и заглавных разрешена границей — это не выдумка."""
    report = checks.compare("завтра встреча в офисе", "Завтра — встреча в офисе.")

    assert report.ok
    assert report.new_words == []


def test_yo_is_not_a_new_word() -> None:
    """«ё» и «е» — одно слово: провайдеры пишут их по-разному."""
    report = checks.compare("все еще думаю", "Всё ещё думаю.")

    assert report.new_words == []
    assert report.ok


def test_punctuation_inside_a_number_is_not_a_change() -> None:
    """Знаки внутри числа расставляет чистка — ей это велено промптом.

    Сравнение по кускам цифр, а не по строке: иначе модель, которая **аккуратнее**
    ставит двоеточие во времени, получила бы худшую оценку — и головное число
    прогона стало бы наградой за небрежность.
    """
    report = checks.compare("встреча 24 08 в 19 40", "Встреча 24.08 в 19:40.")

    assert report.lost_numbers == []
    assert report.new_words == []
    assert report.ok


def test_decimal_separator_is_not_a_change() -> None:
    report = checks.compare("примерно 1 5 часа", "Примерно 1,5 часа.")

    assert report.lost_numbers == []
    assert report.new_words == []


def test_changed_digits_are_still_caught_after_the_loosening() -> None:
    """Нечувствительность к знакам не должна съесть саму проверку чисел."""
    report = checks.compare("напомни в 19:40", "Напомни в 19:04.")

    assert report.lost_numbers == ["19:40"]
    assert not report.ok


def test_added_hyphen_is_not_a_new_word() -> None:
    """«что то» → «что-то» и «по моему» → «По-моему» — это пунктуация, а не выдумка."""
    assert checks.compare("надо что то посмотреть", "Надо что-то посмотреть.").ok
    assert checks.compare("по моему это плохо", "По-моему, это плохо.").ok


def test_catches_lost_latin_name() -> None:
    """Латиница — надёжный случай имени: заглавной в начале фразы она не бывает."""
    report = checks.compare("написать Michael про CV", "Написать про резюме.")

    assert report.lost_names == ["Michael", "CV"]
    assert not report.ok


def test_catches_lost_russian_name_inside_a_phrase() -> None:
    """Заглавная **внутри** фразы в русском бывает только у имени собственного."""
    report = checks.compare("надо позвонить Марине сегодня", "Надо позвонить сегодня.")

    assert report.lost_names == ["Марине"]
    assert not report.ok


def test_capital_at_the_start_is_not_treated_as_a_name() -> None:
    """Первое слово фразы заглавное у всех — иначе замечание было бы в каждой записи.

    Здесь «Ну» — слово-паразит, которое чистка обязана убрать. Признак «слово
    с заглавной» без оглядки на позицию объявил бы его потерянным именем.
    """
    report = checks.compare("Ну надо посмотреть. Потом решу.", "Надо посмотреть. Потом решу.")

    assert report.lost_names == []
    assert report.ok


def test_filler_capitalised_mid_phrase_is_not_a_name() -> None:
    """Расшифровка ставит заглавную после паузы — в том числе внутри фразы.

    Форма фразы наблюдена на живом архиве (прогон замера подставной моделью,
    24.08.2026): «Ну» с большой буквы стояло после запятой в середине
    предложения, и признак «заглавная внутри фразы» объявлял потерянным именем
    слово-паразит, которое чистке убирать **велено промптом**. Четыре записи
    из семидесяти при заведомо законной чистке — то есть каждое двадцатое
    замечание было бы ложным.

    Сама фраза придумана по форме: живой речи владельца в репозитории нет
    (ADR-0002).
    """
    report = checks.compare("надо, соответственно, Ну, посмотреть", "Надо посмотреть.")

    assert report.lost_names == []
    assert report.ok


def test_stretched_interjection_is_not_a_name() -> None:
    """«Э-э» и «Мммм» — одна буква, растянутая паузой, а не имя собственное."""
    report = checks.compare("подумал, Э-э, потом, Мммм, решил", "Подумал, потом решил.")

    assert report.lost_names == []
    assert report.ok


def test_name_survives_in_lower_case() -> None:
    """Имя на месте, но с маленькой буквы — потери смысла нет, замечания тоже."""
    report = checks.compare("поговорить с Мариной", "поговорить с мариной")

    assert report.lost_names == []
    assert report.ok


def test_self_repeat_of_a_name_may_be_dropped() -> None:
    """Самоповтор убирать разрешено: имя обязано уцелеть, а не уцелеть дважды."""
    report = checks.compare("Michael Michael написал", "Michael написал.")

    assert report.lost_names == []
    assert report.ok


def test_dropped_number_is_reported_in_its_original_form() -> None:
    report = checks.compare("встреча 24.08.2026 в 19:40", "Встреча 24.08.2026.")

    assert report.lost_numbers == ["19:40"]


def test_invented_number_is_a_new_word() -> None:
    """Число, которого не было, ловится как выдумка — отдельного поля для него нет."""
    report = checks.compare("напомни вечером", "Напомни в 19:00.")

    assert "19:00" in report.new_words
    assert not report.ok


def test_extra_negation_is_not_a_loss() -> None:
    """Считается потеря, а не разница: лишнее «не» ловится как новое слово."""
    report = checks.compare("решил браться", "Решил не браться.")

    assert report.lost_negations == 0
    assert report.new_words == ["не"]


@pytest.mark.parametrize(
    ("verbatim", "clean", "ratio"),
    [
        ("двенадцать сим", "двенадцать сим", 1.0),
        ("аааа", "аа", 0.5),
        ("", "", 1.0),
    ],
)
def test_length_ratio_is_measured_in_characters(verbatim: str, clean: str, ratio: float) -> None:
    assert checks.compare(verbatim, clean).length_ratio == pytest.approx(ratio)


def test_short_result_is_worth_a_look_but_not_a_violation() -> None:
    """Чистка законно сокращает: «э ну надо это самое посмотреть» → «Надо посмотреть».

    Порог `LENGTH_FLOOR` — повод открыть запись глазами, а не приговор. Смешать
    его с настоящими нарушениями значит утопить их в списке законных сокращений.
    """
    report = checks.compare("э ну надо это самое надо мне посмотреть", "Надо посмотреть.")

    assert report.length_ratio < checks.LENGTH_FLOOR
    assert report.too_short
    assert report.ok


def test_empty_verbatim_does_not_divide_by_zero() -> None:
    assert checks.compare("", "Что-то.").length_ratio == pytest.approx(1.0)
