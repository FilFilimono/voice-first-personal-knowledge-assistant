"""Метрика имён собственных в замере расшифровки — на живой речи.

**Зачем эта метрика вообще существует.** Общий WER отвечает на вопрос «много ли
слов услышано неверно», и на нашей речи он может быть приличным при полностью
испорченных именах: имя — одно слово из сотни. А ломает систему дальше по цепочке
именно оно. Испорченное имя ложится в `transcripts.text` — таблицу, которая
не правится, — и уезжает во всё сразу: в разметку, в чанки, в поиск, в дела
и в названия. Поиск по «GitHub» запись с «Гидхабе» не найдёт никогда.

Живое сравнение 27.08.2026: Handy (локальный Parakeet) слышит «CV чек» верно,
Gemini даёт «Ксюши чеку» и «Сиви-чекку», а GitHub — «Гидхабе». Один вход, разные
движки, разный результат.

**Признак имени не пишется здесь второй раз.** Он уже придуман и починен
в соседнем замере — `evals/cleanup/checks.proper_names`, правка 26.08.2026,
где вслух названа и его цена. Второй признак разошёлся бы с первым молча,
и два замера начали бы считать разное под одним словом «имя». Тест ниже
про мычание проверяет ровно это переиспользование: свой признак «латиница —
имя» на живой записи 60 дал бы ложное срабатывание, а переиспользованный
не даёт.

Тексты берутся из базы по номерам записей (ADR-0002): в репозитории архива нет,
а «похожую» строку выдумать негде.
"""

import re

import pytest

import evals.stt.names as names
from evals.stt.wer import score
from tests.archive import Entry, entry

# Номера записей, а не их текст. Что в каждой — в комментарии: файл должен
# читаться и без базы под рукой.
#
# 1, голос. Содержит «CV» — ровно то слово, на котором движки разошлись живьём.
# Единственное имя собственное записи, 97 слов всего.
CV_VOICE = 1

# 60, голос. Содержит и «rag» (настоящий термин), и «eh» (мычание, записанное
# расшифровкой латиницей). Признак имени обязан различать их — на этой записи
# правило «латиница значит имя» и сломалось бы.
RAG_AND_MYCHANIE = 60

# Наблюдённое искажение, а не выдуманное: так Gemini услышал «CV» в деле 63
# (`docs/debts.md`, верхний раздел).
HEARD_AS = "Сиви"


def _replace_word(text: str, word: str, other: str) -> str:
    """Заменить слово целиком — чтобы не задеть его вхождение внутри другого."""
    return re.sub(rf"\b{re.escape(word)}\b", other, text)


def test_distorted_name_is_counted(archive: list[Entry]) -> None:
    """Имя, услышанное как другое слово, — искажение, и видно, какое именно."""
    reference = entry(archive, CV_VOICE).text
    hypothesis = _replace_word(reference, "CV", HEARD_AS)

    report = names.compare(reference, hypothesis)

    assert report.expected == ["CV"]
    assert report.distorted == ["CV"]
    assert report.share == 1.0


def test_name_heard_correctly_is_not_counted(archive: list[Entry]) -> None:
    """Дословно услышанная запись искажённых имён не содержит."""
    reference = entry(archive, CV_VOICE).text

    report = names.compare(reference, reference)

    assert report.expected == ["CV"]
    assert report.distorted == []
    assert report.share == 0.0


def test_low_wer_hides_a_broken_name(archive: list[Entry]) -> None:
    """**Ради этого метрика и заведена.**

    Одно имя из девяноста семи слов: WER остаётся около процента, то есть
    выглядит отличным, а имя потеряно целиком. Выбирать движок по одному WER
    значит не заметить ровно ту поломку, из-за которой замер и делается.
    """
    reference = entry(archive, CV_VOICE).text
    hypothesis = _replace_word(reference, "CV", HEARD_AS)

    assert score(reference, hypothesis).wer < 0.05
    assert names.compare(reference, hypothesis).share == 1.0


def test_latin_mychanie_is_not_a_name(archive: list[Entry]) -> None:
    """Признак имени берётся у соседнего замера, а не пишется заново.

    На записи 60 расшифровка записала мычание владельца как «eh». Правило
    «латиница — имя» объявило бы его именем и, потеряв, дало бы ложное
    замечание; правило из `checks` (латиница от трёх букв либо целиком
    заглавными) — не даёт.
    """
    reference = entry(archive, RAG_AND_MYCHANIE).text
    without_mychanie = _replace_word(reference, "eh", "")

    assert names.compare(reference, without_mychanie).distorted == []


def test_real_term_is_a_name(archive: list[Entry]) -> None:
    """Обратная сторона того же правила: «rag» — термин, и его потеря видна."""
    reference = entry(archive, RAG_AND_MYCHANIE).text
    without_term = _replace_word(reference, "rag", "")

    assert names.compare(reference, without_term).distorted == ["rag"]


def test_corpus_share_counts_names_not_records() -> None:
    """Доля по корпусу считается по именам, а не усреднением по записям.

    Иначе запись с одним потерянным именем весила бы столько же, сколько
    запись с четырьмя целыми, и корпус из двадцати голосовых показывал бы
    не то, что в нём происходит. Та же причина, по которой `corpus_wer`
    складывает ошибки и слова, а не усредняет WER.
    """
    broken = names.Names(expected=["GitHub"], distorted=["GitHub"])
    intact = names.Names(expected=["CV", "Discord", "Ozon", "Michael"], distorted=[])

    assert names.corpus_share([broken, intact]) == pytest.approx(0.2)


def test_corpus_without_names_is_not_a_failure() -> None:
    """Корпус без единого имени — ноль, а не единица.

    Деление на ноль здесь не абстракция: корпус собирается владельцем,
    и первые прогоны могут не содержать ни одного термина.
    """
    assert names.corpus_share([names.Names(expected=[], distorted=[])]) == 0.0
    assert names.corpus_share([]) == 0.0
