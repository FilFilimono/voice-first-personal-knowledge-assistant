"""Проверка ответа модели перед показом: что именно считается расхождением.

Гвард — цена, которой куплена ось 1 ADR-0015. Модель пишет текст, который
читает владелец, значит в единственное ежедневное сообщение входит
недетерминированность; съедена она проверкой, а не обещанием. Любое
расхождение — прежний сухой рендер, без второй попытки.

Поэтому здесь нет ни одного теста про качество текста: качество меряется
`just eval-digest` на живой модели, а гвард отвечает ровно на один вопрос —
**показывать это владельцу или нет.** Ни сети, ни базы файлу не нужно.

Отдельно про направление ошибок. Ложный отказ стоит сухой сводки — владелец
получит прежнее сообщение. Ложное согласие стоит показанного упрёка или
выдуманной даты. Цены несимметричны, и тесты закрепляют перекос в сторону
отказа: сомнительное отвергается.
"""

import pytest

from brain.core.present import guard

# Идентификатор дела по умолчанию: тот же, что в примерах плана волны.
DEED = 7

# Строки без цифр и без стоп-слов — чтобы каждый тест падал по своей причине,
# а не по чужой. Числа и упрёк вносятся в текст тестом, которому они нужны.
CLEAN_LEAD = "Фильм лежит дольше прочего — досмотреть или снять?"
CLEAN_LINE = "Досмотреть фильм"


def digest_text(
    *,
    lead: str = CLEAN_LEAD,
    ids: tuple[int, ...] = (DEED,),
    line: str = CLEAN_LINE,
    loop_line: str | None = None,
) -> guard.Written:
    """Ответ модели: лид, строка на каждое дело, необязательная строка петли.

    Нумерация, порядок и кнопки в него не входят — они остаются кодом
    (ADR-0015, п. 5), и гвард их не видит.
    """
    return guard.Written(
        lead=lead,
        lines=dict.fromkeys(ids, line),
        loop_line=loop_line,
    )


def written_with_ids(ids: list[int]) -> guard.Written:
    """Ответ, в котором интересны только идентификаторы дел."""
    return digest_text(ids=tuple(ids))


# --- Идентификаторы: ровно те, что дали ---


def test_accepts_an_answer_that_matches() -> None:
    """Ответ без расхождений проходит — иначе гвард просто выключил бы модель."""
    assert (
        guard.check(
            written_with_ids([DEED]), expected_ids=[DEED], source_digits=set(), word_budget=80
        )
        is None
    )


def test_rejects_unknown_id() -> None:
    """Модель написала про дело, которого ей не давали."""
    assert (
        guard.check(
            written_with_ids([7, 99]), expected_ids=[7], source_digits=set(), word_budget=80
        )
        is not None
    )


def test_rejects_lost_id() -> None:
    assert (
        guard.check(written_with_ids([7]), expected_ids=[7, 8], source_digits=set(), word_budget=80)
        is not None
    )


def test_reason_names_the_id_that_did_not_match() -> None:
    """Причину читает не владелец, а журнал: по ней надо понять, что случилось."""
    reason = guard.check(
        written_with_ids([7, 99]), expected_ids=[7], source_digits=set(), word_budget=80
    )

    assert reason is not None
    assert "99" in reason


def test_rejects_a_blank_line_for_a_deed() -> None:
    """Пустая строка — то же потерянное дело, только незаметное глазу.

    Идентификатор на месте, показывать нечего: у дела в сводке остался бы
    голый номер.
    """
    assert (
        guard.check(
            digest_text(line="   "), expected_ids=[DEED], source_digits=set(), word_budget=80
        )
        is not None
    )


def test_rejects_an_empty_lead() -> None:
    """Лид — единственное, ради чего модель зовут: он называет одну вещь.

    Без него сводка снова пересказ списка, и сухой рендер тогда честнее.
    """
    assert (
        guard.check(
            digest_text(lead="  "), expected_ids=[DEED], source_digits=set(), word_budget=80
        )
        is not None
    )


def test_zero_deeds_is_not_the_guards_argument() -> None:
    """Ноль дел гвард не обсуждает: ноль данных — ноль вызовов (ADR-0015, п. 5).

    Такой ответ до него не доезжает, и выдумывать ему запрет не из чего.
    """
    assert (
        guard.check(digest_text(ids=()), expected_ids=[], source_digits=set(), word_budget=80)
        is None
    )


# --- Цифры: только те, что были во входе ---


def test_rejects_invented_digit() -> None:
    """Дата, которой не было во входе, — выдумка, а не связка."""
    written = digest_text(lead="Досмотреть фильм до 15 сентября")
    assert guard.check(written, expected_ids=[7], source_digits={"7"}, word_budget=80) is not None


def test_keeps_a_digit_that_came_from_the_input() -> None:
    """Число из входа — не выдумка: возраст дела назвать можно и нужно."""
    written = digest_text(lead="Фильм переносится 15 дней — досмотреть или снять?")
    assert guard.check(written, expected_ids=[DEED], source_digits={"15"}, word_budget=80) is None


def test_a_number_is_compared_whole() -> None:
    """«15» из «2015» не выводится: год во входе не разрешает половину года."""
    written = digest_text(lead="Досмотреть фильм до 15 числа")
    assert (
        guard.check(written, expected_ids=[DEED], source_digits={"2015"}, word_budget=80)
        is not None
    )


def test_a_digit_inside_a_word_counts_too() -> None:
    """Цифра, приросшая к слову, остаётся числом, которого модели не давали."""
    written = digest_text(lead="Досмотреть фильм по плану Б2 — или снять?")
    assert (
        guard.check(written, expected_ids=[DEED], source_digits=set(), word_budget=80) is not None
    )


def test_digits_are_checked_in_every_line() -> None:
    """Выдумка в строке дела не безопаснее выдумки в лиде."""
    written = digest_text(line="Досмотреть фильм за 40 минут")
    assert (
        guard.check(written, expected_ids=[DEED], source_digits=set(), word_budget=80) is not None
    )


# --- Бюджет: сорок секунд под тестом, а не в пожелании ---


def test_rejects_over_budget() -> None:
    assert (
        guard.check(
            digest_text(lead="слово " * 200), expected_ids=[7], source_digits=set(), word_budget=80
        )
        is not None
    )


def test_budget_counts_the_whole_message() -> None:
    """Считается всё, что увидит владелец, а не один лид.

    Лид в пределах, а сообщение целиком — нет: разложить перерасход по строкам
    дел значит обойти бюджет, не нарушив ни одной проверки.
    """
    written = digest_text(lead="слово " * 30, ids=(7, 8, 9), line="слово " * 30)
    assert (
        guard.check(written, expected_ids=[7, 8, 9], source_digits=set(), word_budget=80)
        is not None
    )


def test_budget_edge_passes() -> None:
    """Ровно бюджет — это в пределах: граница включена, иначе она не проверяема."""
    written = digest_text(lead="слово " * 79, line="слово")
    assert guard.check(written, expected_ids=[DEED], source_digits=set(), word_budget=80) is None


def test_loop_line_is_inside_the_budget() -> None:
    """Строка петли — часть того же сообщения, а не приложение к нему."""
    written = digest_text(loop_line="слово " * 100)
    assert (
        guard.check(written, expected_ids=[DEED], source_digits=set(), word_budget=80) is not None
    )


# --- Упрёк: по тексту, а не по намерению ---


def test_catches_reproach_in_the_lead() -> None:
    """Упрёк ловится в тексте, а не в намерении разработчика.

    `revision_after_use.md`: правило §7.0 п.2 не нарушено в замысле — оно
    нарушено в реализации, и поймать это должен тест по тексту.
    """
    assert guard.reproach("У тебя накопилось три дела") is not None
    assert guard.reproach("Досмотреть фильм или снять его со списка?") is None


def test_friction_with_a_choice_is_allowed() -> None:
    """Трение назвать можно — но только вместе с выбором."""
    assert guard.reproach("Фильм переносится третий день — досмотреть или снять?") is None


def test_reproach_returns_the_word_it_found() -> None:
    """Возвращается найденное слово: без него причина в журнале не проверяема."""
    assert guard.reproach("У тебя накопилось три дела") == "накопилось"


def test_reproach_ignores_case() -> None:
    assert guard.reproach("Накопилось многое") is not None


def test_a_question_without_a_choice_is_still_a_reproach() -> None:
    """Вопросительный знак сам по себе выбора не даёт."""
    assert guard.reproach("Ты так и не досмотрел фильм?") is not None


def test_a_choice_without_a_question_is_still_a_reproach() -> None:
    """И «или» само по себе тоже: «не сделал ни того, ни другого» — упрёк."""
    assert guard.reproach("Ты так и не досмотрел фильм или сериал.") is not None


def test_the_choice_has_to_stand_in_the_same_line() -> None:
    """Выбор строкой ниже упрёк не снимает: владелец читает строку целиком.

    Правило намеренно строгое — цена ложного отказа сухая сводка, цена
    пропуска упрёк в единственном ежедневном сообщении.
    """
    assert guard.reproach("Ты так и не досмотрел фильм.\nДосмотреть или снять?") is not None


def test_a_stop_word_needs_the_conjunction_not_just_the_letters() -> None:
    """«или» внутри слова выбором не считается."""
    assert guard.reproach("Ты забыл, как тебя молили?") is not None


def test_empty_text_has_no_reproach() -> None:
    assert guard.reproach("") is None


def test_check_looks_for_reproach_in_every_line() -> None:
    """Стоп-детектор проходит и по лиду, и по строкам дел (ADR-0015, п. 5)."""
    assert (
        guard.check(
            digest_text(line="Ты забыл про фильм"),
            expected_ids=[DEED],
            source_digits=set(),
            word_budget=80,
        )
        is not None
    )


def test_check_looks_for_reproach_in_the_loop_line() -> None:
    written = digest_text(loop_line="Этот вопрос висит с весны")
    assert (
        guard.check(written, expected_ids=[DEED], source_digits=set(), word_budget=80) is not None
    )


@pytest.mark.parametrize(
    "word",
    ["накопилось", "не сделал", "до сих пор не", "так и не", "опять не", "снова не", "забыл"],
)
def test_stop_words_are_the_ones_from_the_rule(word: str) -> None:
    """Список закрыт и лежит на виду: он читается вместе с §7.0, а не в коде."""
    assert word in guard.STOP_WORDS


def test_stop_words_include_the_hanging_one() -> None:
    """«висит с» — про давность, то есть ровно ретроспективный упрёк."""
    assert "висит с" in guard.STOP_WORDS
