"""Утренняя сводка: что сегодня висит.

`vision.md` §5.1: календарь на сегодня → незакрытые обещания → **одна** открытая
петля → вчерашнее неразобранное. Календаря до Фазы 5 нет, поэтому первая строка
просто отсутствует, а не заменяется заглушкой.

**Почему сводка не подпадает под запрет ретроспективного упрёка**, хотя
и перечисляет несделанное. Она отдаёт день вперёд — «вот что сегодня висит», —
а не подводит итог прошедшему. Разница не в списке, а в том, к какому времени
он относится (`vision.md` §5.1, §7.0).

**Почему без вызова модели.** Содержание — три списка из базы. Модель добавила
бы связки, а взамен принесла бы недетерминированность в единственное сообщение,
которое приходит каждый день, и сделала бы его непроверяемым быстрым тестом
(ADR-0013, ось 4). Это открытый вопрос с назначенным сроком проверки: через две
недели использования владелец отвечает, читает ли он сводку.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from brain.core.digest import commitments as commitment_store
from brain.core.digest import evening, loops, slots
from brain.storage.models.commitment import Commitment
from brain.storage.models.loop import Loop

# Сколько дел показывать. «Читается за 40 секунд» из `vision.md` §5.1 —
# это обещание, и список на двадцать пунктов его нарушает молча. Показываются
# старейшие: эскалация вверх по важности, а не по частоте (§7).
COMMITMENTS_SHOWN = 5

# Сколько вчерашних спорных записей упоминать. Сводка про них только
# напоминает — разбирает их вечер, и дублировать всю пачку с утра незачем.
UNSORTED_SHOWN = 3


@dataclass(frozen=True, slots=True)
class Morning:
    """Содержание утренней сводки. Пустое — значит молчим."""

    commitments: list[Commitment]
    open_commitments: int
    loop: Loop | None
    unsorted: list[evening.Disputed]

    @property
    def empty(self) -> bool:
        """Нечего дать — не отправляем ничего (`vision.md` §7.0, принцип 3).

        **Неразобранное само по себе содержанием не считается**, и это найдено
        повтором по живому архиву 19 августа 2026: сводка 17 августа состояла
        целиком из строки «Со вчера ждут разбора: 2». Сообщение, у которого всё
        содержание — указание на несделанное, нарушает принцип 1 («сообщение
        даёт, а не требует») ровно тем, чем нарушал бы вечерний упрёк.

        Поэтому строка про неразобранное — приписка к сводке, а не повод её
        отправить: она приходит, когда есть что дать, и молчит, когда нечего.
        """
        return not self.commitments and self.loop is None

    @property
    def loop_stuck(self) -> bool:
        """Петлю уже трижды откладывали — `vision.md` §5.2 велит сменить вопрос.

        Признак живёт здесь, а не у каждого читателя, потому что читателей два
        и они обязаны сказать одно и то же: текст сводки (`render.morning`)
        и кнопки под ней (через нагрузку касания в `service._prepare`).
        Посчитай его каждый сам — и разъехаться они смогут молча, а это ровно
        тот почерк, которым в проекте уже потерялась `loops.is_stuck`.
        """
        return self.loop is not None and loops.is_stuck(self.loop.times_asked)


async def collect(now: datetime, timezone: str) -> Morning:
    """Собрать сводку на сегодня. Ничего не отправляет и ничего не помечает."""
    open_now = await commitment_store.open_ones()
    picked = await loops.pick(now)

    yesterday = slots.local_date(now, timezone) - timedelta(days=1)
    unsorted = [
        item
        for item in await evening.disputed(limit=UNSORTED_SHOWN * 3)
        if slots.local_date(item.created_at, timezone) == yesterday
    ]

    return Morning(
        commitments=open_now[:COMMITMENTS_SHOWN],
        open_commitments=len(open_now),
        loop=picked,
        unsorted=unsorted[:UNSORTED_SHOWN],
    )
