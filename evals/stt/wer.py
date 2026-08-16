"""WER — доля слов, которые модель услышала неправильно.

Считается редакционным расстоянием по словам: сколько замен, вставок
и удалений нужно, чтобы превратить расшифровку в эталон, делённое на число
слов эталона. Ноль — совпало дословно, единица — не угадано ни слова.

Своя реализация на тридцать строк вместо зависимости: алгоритм школьный,
а лишняя библиотека ради него — это ADR и ещё одна версия, которую надо
держать актуальной.
"""

import re
from dataclasses import dataclass

# Пунктуация и регистр не сравниваются: для поиска и разметки они не важны,
# а провайдеры расставляют их по-разному и меняют это между версиями.
WORD = re.compile(r"[\w']+", re.UNICODE)


def words(text: str) -> list[str]:
    """Разбить строку на слова, приведя к сравнимому виду."""
    return WORD.findall(text.lower().replace("ё", "е"))


@dataclass(frozen=True, slots=True)
class Score:
    """Результат сравнения одной расшифровки с эталоном."""

    errors: int
    reference_words: int

    @property
    def wer(self) -> float:
        if self.reference_words == 0:
            return 0.0 if self.errors == 0 else 1.0
        return self.errors / self.reference_words


def score(reference: str, hypothesis: str) -> Score:
    """Сравнить расшифровку с эталоном."""
    expected = words(reference)
    actual = words(hypothesis)

    # Классическая динамика: distance[i][j] — расстояние между первыми i словами
    # эталона и первыми j словами расшифровки.
    previous = list(range(len(actual) + 1))
    for i, expected_word in enumerate(expected, start=1):
        current = [i]
        for j, actual_word in enumerate(actual, start=1):
            substitution = previous[j - 1] + (expected_word != actual_word)
            deletion = previous[j] + 1
            insertion = current[j - 1] + 1
            current.append(min(substitution, deletion, insertion))
        previous = current

    return Score(errors=previous[-1], reference_words=len(expected))


def corpus_wer(scores: list[Score]) -> float:
    """WER по корпусу.

    Ошибки и слова складываются по всему набору, а не усредняются по записям:
    иначе одна короткая фраза с двумя ошибками весила бы столько же,
    сколько минутная запись с двумя.
    """
    errors = sum(item.errors for item in scores)
    total = sum(item.reference_words for item in scores)
    return Score(errors=errors, reference_words=total).wer
