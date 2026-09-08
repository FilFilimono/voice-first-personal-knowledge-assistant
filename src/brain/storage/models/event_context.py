"""Контекст перед событием: что и когда мы про него сказали.

`vision.md` §5.7 — три слоя за 15–30 минут до встречи. Заказанное касание,
вне бюджета трёх инициативных (§7.1): владелец о нём просил сам, и молчать
о заказанном система права не имеет.

**Таблица нужна ради одного: не сказать одно и то же трижды.** Тик идёт раз
в пять минут, окно перед событием — пятнадцать; без отметки владелец получил
бы три одинаковых сообщения на одну встречу.

**Отметок две, и это прямое следствие находки 7 сентября 2026.** В тот день
выяснилось, что `touches.sent_at` пишется **до** отправки и означает «слот
занят», а не «сообщение дошло»: упавшая отправка выглядела в журнале точно
так же, как состоявшаяся, и сутки сгорали молча — с подтверждением в базе.
Прибор, который врёт, хуже отсутствующего прибора.

Поэтому здесь:

    decided_at     тик дошёл до этого события и взялся за него. Пишется
                   до отправки, и означает ровно это — намерение;
    delivered_at   Telegram принял сообщение. Пишется **после** успешной
                   отправки, вместе с `message_id`, и означает доставку.

Строка с пустым `delivered_at` — это **честное «не дошло»**, и следующий тик
имеет право попробовать снова, пока окно не закрылось. Ровно так же устроен
`silence_reason = failed` у слотов: отказ стоит пяти минут, а не суток.

Цена названа вслух: если Telegram сообщение принял, а ответ до нас не дошёл,
владелец получит контекст дважды. Это дешевле тишины перед встречей и,
в отличие от неё, заметно.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from brain.storage.models.base import Base


class EventContext(Base):
    """Один контекст, показанный (или не показанный) перед одним событием."""

    __tablename__ = "event_contexts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Идентификатор вхождения, а не правила повторения: события приходят
    # развёрнутыми (`singleEvents=true`), и у каждой еженедельной планёрки
    # свой идентификатор. Иначе контекст пришёл бы к первой встрече серии
    # и больше никогда.
    google_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    calendar_id: Mapped[str] = mapped_column(Text, nullable=False)

    # Когда начинается сама встреча. Нужно, чтобы решить, не закрылось ли окно:
    # неотправленный контекст к уже начавшейся встрече отправлять поздно,
    # и повтор обязан это понимать.
    event_starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Название — для `/status`. По нему не ищут: смотрят глазами.
    event_title: Mapped[str] = mapped_column(Text, nullable=False)

    # Намерение. Пишется до отправки — см. шапку модуля.
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Доставка. Пишется **после** успешной отправки, и только тогда.
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Идентификатор сообщения в Telegram — единственное доказательство
    # доставки, которое остаётся в базе.
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
