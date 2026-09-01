"""Привычка — то, что делается регулярно, а не один раз.

`vision.md` §5.11. От задачи отличается не сроком, а тем, что у привычки **нет
состояния «сделано»**: выполненная сегодня, завтра она наступает снова.

**Почему это не дело и не строка в `commitments`.** Дело закрывается один раз;
привычка закрывать нечего. Оставленная в общем списке, она повела бы себя одним
из двух неверных способов: висела бы вечно, превращаясь в упрёк, либо исчезала
после первого выполнения. Поэтому в список незакрытых обещаний привычка
не попадает вовсе — не как исключение показа, а потому, что дела из единиц
типа `habit` не заводятся (`digest.commitments.FROM_TYPE`).

**Чего в этой таблице нет и не будет.** Ни счётчика пропусков, ни серии,
ни «дней подряд». Не выполненная вчера привычка — не долг (`vision.md` §5.11
и §7.0, принципы 2 и 5): она просто наступает снова, в свой срок, и вчерашнее
не упоминается. Колонка, посчитавшая пропуски, однажды прочиталась бы вслух —
других способов у числа в базе не бывает.

`last_reminded_at` этим счётчиком **не является**: она отвечает на вопрос
«сегодня уже напоминали?» и нужна затем, чтобы тик раз в пять минут не прислал
одно и то же тридцать шесть раз. По ней не считается ничего и она не
показывается никогда.

**Ритм хранится тем, чем его назвал владелец.** «Два раза в неделю» и «по
понедельникам и четвергам» — разные фразы, даже когда дни совпали; вернуть
владельцу надо его формулировку, а не нашу.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class Rhythm(enum.StrEnum):
    """Как часто наступает привычка. Значения английские — это значения колонки.

    Живёт здесь, а не в разборе (`core/scheduler/recurrence`), по той же
    причине, по которой `AnnotationType` живёт в `storage`, а его русские
    определения — в промпте: enum это колонка, а не деталь разбора. Разбор
    импортирует её отсюда, а не наоборот.

    Шести значений хватает на всё, что `vision.md` §5.11 называет ритмом.
    Седьмого («раз в месяц») здесь нет намеренно: в замысле его нет, а значение
    enum'а, добавленное на будущее, убрать уже нельзя — `ALTER TYPE` умеет
    только добавлять.
    """

    DAILY = "daily"
    EVERY_OTHER_DAY = "every_other_day"
    WEEKDAYS = "weekdays"
    WEEKENDS = "weekends"
    # Названные дни недели: «по понедельникам и четвергам».
    WEEKLY_DAYS = "weekly_days"
    # «Два раза в неделю»: сколько раз — сказал владелец, а в какие дни —
    # посчитал код при подтверждении.
    TIMES_PER_WEEK = "times_per_week"


class HabitStatus(enum.StrEnum):
    """Жива ли привычка.

    Двух значений хватает, и третьего («на паузе») здесь нет намеренно: пауза
    в этой системе общая (`/pause`), а привычка, которую поставили на паузу
    отдельно, — это ещё один способ забыть, что она вообще есть.
    """

    ACTIVE = "active"
    STOPPED = "stopped"


class Habit(Base):
    """Одна привычка: что делать и по какому ритму напоминать."""

    __tablename__ = "habits"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_entry_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("entries.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Единица, из которой привычка выведена. SET NULL, как у дела: единицы
    # уходят вместе с разметкой при перепрогоне, а привычка её переживает.
    source_unit_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("entry_units.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Что именно делать — снимок слов владельца без ритма и без «хочу».
    # Снимок, а не отрезок: перепрогон расшифровки сдвигает границы, а привычка
    # обязана продолжать напоминать (та же причина, что у `commitments.text`).
    text: Mapped[str] = mapped_column(Text, nullable=False)

    rhythm: Mapped[Rhythm] = mapped_column(
        Enum(Rhythm, name="habit_rhythm", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    # Дни недели, когда ритм их называет: 0 — понедельник. У «каждый день»
    # и «через день» пусто.
    days: Mapped[list[int] | None] = mapped_column(ARRAY(SmallInteger), nullable=True)
    # Сколько раз в неделю сказал владелец. Хранится ради его же формулировки:
    # дни при этом уже посчитаны и лежат рядом.
    times_per_week: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    # Когда напоминать — по календарю владельца. Час обязателен, минута почти
    # всегда ноль: «каждый день в 9», а не «в 9:07».
    hour: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    minute: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)

    status: Mapped[HabitStatus] = mapped_column(
        Enum(HabitStatus, name="habit_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=HabitStatus.ACTIVE,
    )

    # Когда напоминали в последний раз. Не счётчик — см. модуль.
    last_reminded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Одна привычка на запись. Кнопка подтверждения живёт в переписке вечно,
        # и второе нажатие не должно завести второе ежедневное сообщение —
        # та же защита, что у петли, только схемой, а не проверкой в коде.
        UniqueConstraint("source_entry_id", name="uq_habits_entry"),
        # «Кому сегодня пора» — запрос тика, он идёт по статусу.
        Index("ix_habits_active", "status", "id"),
    )
