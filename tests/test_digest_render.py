"""Как утренняя сводка звучит словами.

Быстрый тест без базы: рендер — чистая функция над готовым содержанием.
Проверяется вопрос под петлёй, потому что он единственный в сводке зависит
от состояния петли, а не только от её текста.
"""

from brain.core.digest import loops, render
from brain.core.digest.morning import Morning
from brain.storage.models.loop import Loop


def _summary(times_asked: int) -> Morning:
    return Morning(
        commitments=[],
        open_commitments=0,
        loop=Loop(id=1, question="куда дальше", times_asked=times_asked),
    )


def _loop_question(content: Morning) -> str:
    """Строка под петлёй — та, что идёт сразу за самим вопросом."""
    lines = render.morning(content).splitlines()
    index = next(number for number, line in enumerate(lines) if line.startswith("🔁"))
    return lines[index + 1]


def test_fresh_loop_is_asked_whether_it_moved() -> None:
    assert _loop_question(_summary(times_asked=0)) == "Сдвинулось?"


def test_thrice_snoozed_loop_is_asked_to_close_or_rephrase() -> None:
    """После третьего «ещё думаю» текст спрашивает то же, что и кнопки.

    «Сдвинулось?» здесь было бы вопросом, на который владелец уже трижды
    ответил «ещё думаю», — то есть мягким ретроспективным упрёком
    (`vision.md` §7.0, принцип 1).
    """
    content = _summary(times_asked=loops.SNOOZES_BEFORE_DECISION)

    assert _loop_question(content) == "Закрыть или переформулируешь?"
    assert "Сдвинулось?" not in render.morning(content)
