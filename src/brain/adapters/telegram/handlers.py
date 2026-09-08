"""Обработчики сообщений.

Порядок в каждом обработчике один и тот же и выбран не случайно:

    подтверждение → транзакция → правка подтверждения

Подтверждение уходит первым, потому что оно должно прийти быстрее 300 мс:
мысль записывается на ходу, и ожидание ответа — единственное, что заставляет
владельца смотреть в телефон дольше, чем нужно.

Идентификатор подтверждения нужен внутри задания, а до отправки его
не существует — отсюда и порядок. Инвариант 7 при этом цел: подтверждение
лежит **вне** транзакции, а запись и задание пишутся вместе. Если транзакция
упала, то же самое сообщение превращается в объяснение, и молчания не возникает.
"""

import logging
import re
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import brain.core.retrieval.embeddings as embeddings
from brain.adapters.telegram import digest_keyboards, search_text, status_text
from brain.adapters.telegram import notifier as notifier_module
from brain.adapters.telegram.notifier import (
    ANNOTATION_PREFIX,
    CALENDAR_PREFIX,
    CALLBACK_PREFIX,
    NO,
    PRIVATE,
    SET,
    WRONG,
    calendar_keyboard,
    yes_no_keyboard,
)
from brain.config import Settings
from brain.core import digest, enrich, ingest, llm
from brain.core import status as core_status
from brain.core.llm import limits
from brain.core.memory import overrides, repository
from brain.core.memory.unit_of_work import capture_transaction
from brain.core.present import archive_question, archive_topics
from brain.core.present import entry as entry_view
from brain.core.retrieval import search
from brain.core.scheduler import recurrence, reminders
from brain.integrations.gcal import events as calendar
from brain.storage.models.annotation import AnnotationType
from brain.storage.models.commitment import CommitmentState
from brain.storage.models.habit import Rhythm
from brain.storage.models.loop import LoopStatus
from brain.storage.models.override import OverrideAction

logger = logging.getLogger(__name__)

ACK = "Принял."
STORED = "Записал."
CAPTURE_FAILED = "Не смог сохранить — повторите, пожалуйста."
ALREADY_SCHEDULED = "Это напоминание уже заведено."
QUESTION_LOST = "Не нашёл запись, к которой был вопрос — посмотрите логи."

# Что показать после нажатия «в календарь». Владелец обязан увидеть **что
# именно** ушло: текст, время и имя календаря — это требование приёмки Фазы 5,
# а не вежливость. Молча события не создаются.
CALENDAR_ADDED = "🗓 {title}\n{when}\n\nУшло в календарь «{calendar}»."
CALENDAR_ALREADY = "🗓 Это уже в календаре."
# Отказ по приватности выглядит как отказ, а не как поломка: так и задумано
# (инвариант 5). Кнопки у приватной записи нет, но нажать старую можно —
# приватность включается задним числом.
CALENDAR_PRIVATE = "🔒 Приватная запись наружу не уходит. В календарь — тоже."
CALENDAR_OFF = "Календарь не настроен. Процедура целиком — docs/google_calendar_setup.md."
CALENDAR_NO_REMINDER = "У этой записи нет напоминания — в календарь класть нечего."
# Запись цела — это первое, что владелец должен прочитать: отказ Google
# не имеет права выглядеть как потеря мысли.
CALENDAR_FAILED = "Не получилось создать событие. Запись цела, напоминание работает.\n\n{detail}"
PRIVATE_VOICE_REFUSED = (
    "Приватный режим — только текст.\n"
    "Расшифровка идёт через внешний сервис, поэтому голосовое пришлось бы "
    "отправить наружу. Наберите текстом."
)
SEARCH_FAILED = "Поиск не отработал — посмотрите логи. Записи целы."

# Тексты среза по темам. Молчание здесь — обычный исход: тем меньше двух
# означает, что срезом это не назовёшь (`core/digest/by_topic`).
NO_TOPICS = (
    "Пока не из чего собрать срез: тем за последние дни меньше двух.\n"
    "Это не поломка — темы появляются из разметки, когда материал разнообразнее."
)
NO_SUCH_TOPIC = "Не нашёл, что вспомнить по теме «{topic}».\n\nЗа последние дни были: {known}."

# Сколько результатов показывать. Пять — потому что recall меряется на пяти
# (ADR-0006), и показывать больше, чем меряешь, значит не знать цену выдачи.
RESULTS = 5

HELP = (
    "Пишите или наговаривайте — сохраню.\n"
    "Скажите «напомни завтра в 10 …» — напомню.\n"
    # Примеры фраз, а не список функций (`vision.md` §14): списки не читают.
    "Спросите «что там по учёбе» или «что я думал про работу» — расскажу, "
    "что записано. Сообщение при этом всё равно сохранится.\n\n"
    "/find <что искать> — поиск по архиву\n"
    "/recall — о чём вы думали за неделю; /recall <тема> — вспомнить подробно\n"
    # Якорь объясняется примером, а не описанием команды: в меню Telegram его
    # не положить (номер — часть самой команды), и увидит его владелец в конце
    # цитаты. Одна строка тут нужна затем, чтобы первый раз он понял, что это.
    "/e42 — показать запись целиком; такой номер стоит в конце каждой цитаты\n"
    "/status — всё ли работает\n"
    "/private on — приватный режим: записи не уходят наружу, голосовые "
    "не принимаются\n"
    "/private off — обычный режим\n"
    "/private — что сейчас\n\n"
    "/pause — замолчать на неделю\n"
    "/dayoff — сегодня выходной\n"
    "/resume — снова говорить"
)

# Тексты про тишину. Главное в них — вторая строка: система обязана сказать,
# что именно продолжит приходить. Иначе «замолчи» читается как «выключить»,
# и владелец перестанет им пользоваться из страха пропустить напоминание.
PAUSED = "🔇 Молчу до {until:%d.%m}.\n\nНапоминания, которые вы поставили, будут приходить."
DAY_OFF = "🔇 Сегодня выходной.\n\nНапоминания, которые вы поставили, будут приходить."
RESUMED = "🔊 Снова говорю."
NOT_QUIET = "Я и так не молчу."


def build_router(settings: Settings) -> Router:
    """Собрать роутер. Настройки замыкаются, а не читаются в каждом обработчике."""
    router = Router(name="capture")
    audio_dir = Path(settings.audio_dir)

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(HELP)

    @router.message(Command("status"))
    async def status(message: Message) -> None:
        """Что сейчас происходит — чтобы не лезть в терминал с телефона."""
        collected = await core_status.collect(
            settings.brain_timezone,
            # Лимиты провайдер держит на модель, поэтому наружу уходят поправки
            # владельца, привязанные к именам моделей, а не два числа на тир:
            # известные лимиты знает `core/llm/limits`, а `.env` их перебивает.
            quota_overrides=limits.configured(settings),
        )
        await message.answer(status_text.render(collected, timezone=settings.brain_timezone))

    @router.message(Command("private"))
    async def private_mode(message: Message, command: CommandObject) -> None:
        argument = (command.args or "").strip().lower()

        if argument not in {"on", "off"}:
            state = "включён" if await repository.is_private_mode() else "выключен"
            await message.answer(f"Приватный режим {state}.\n\n{HELP}")
            return

        enabled = argument == "on"
        await repository.switch_private_mode(enabled=enabled)
        await message.answer(
            "Приватный режим включён. Голосовые не принимаю — только текст."
            if enabled
            else "Приватный режим выключен."
        )

    @router.message(Command("pause"))
    async def pause(message: Message) -> None:
        """«Замолчи на неделю» (`vision.md` §7)."""
        until = await digest.quiet.pause(datetime.now(UTC))
        await message.answer(
            PAUSED.format(until=until.astimezone(ZoneInfo(settings.brain_timezone)))
        )

    @router.message(Command("dayoff"))
    async def day_off(message: Message) -> None:
        """«Сегодня выходной» — до полуночи у владельца, а не в UTC."""
        await digest.quiet.day_off(datetime.now(UTC), settings.brain_timezone)
        await message.answer(DAY_OFF)

    @router.message(Command("resume"))
    async def resume(message: Message) -> None:
        now = datetime.now(UTC)
        if not await digest.quiet.is_quiet(now):
            await message.answer(NOT_QUIET)
            return
        await digest.quiet.resume(now)
        await message.answer(RESUMED)

    @router.message(Command("find"))
    async def find(message: Message, command: CommandObject) -> None:
        """Поиск по архиву.

        Командой, а не просто словом «найди» в начале сообщения. Обычное
        сообщение обязано всегда записываться: перехват сделал бы захват
        капризным — а захват это единственное, что в этой системе не имеет
        права работать через раз.
        """
        query = (command.args or "").strip()
        if not query:
            await message.answer(search_text.EMPTY_QUERY)
            return

        # В приватном режиме поиск идёт **без вектора** — решение владельца
        # 07.09.2026. Отказывать целиком не надо: два канала из трёх считаются
        # внутри Postgres и наружу не ходят. Эмбеддер при этом не строится
        # вовсе: ветка обязана быть здесь, а не внутри `search`, иначе «наружу
        # не пошли» держалось бы на честном слове аргумента.
        private = await repository.is_private_mode()

        try:
            found = (
                await search.local(query, limit=RESULTS)
                if private
                else await search.search(embeddings.build_embedder(settings), query, limit=RESULTS)
            )
        except Exception:
            # Поиск ходит наружу за вектором запроса, и провайдер падает.
            # Молчать нельзя: владелец решит, что в архиве ничего нет.
            logger.exception("поиск не отработал: %r", query)
            await message.answer(SEARCH_FAILED)
            return

        await message.answer(
            search_text.render(query, found, today=datetime.now(UTC), local=private)
        )

    @router.message(Command(re.compile(rf"{entry_view.PREFIX}(\d+)")))
    async def show_entry(message: Message, command: CommandObject) -> None:
        """Запись целиком по якорю из цитаты — `/e42`.

        **Регистрируется до `F.text`**, иначе `/e42` легло бы в архив записью:
        роутер идёт по порядку, и обработчик, стоящий ниже общего фильтра,
        не получит ничего. Ровно так же стоят `/find` и `/recall`.

        Регулярным выражением, а не списком команд: номер записи — часть самой
        команды, и это единственный способ сделать якорь **тапабельным**.
        Telegram превращает `/слово` в ссылку сам, но только если это `/слово`,
        а не `/слово 42` — команда с аргументом набирается руками, а якорь,
        который надо набирать, работать не будет (`present/entry`).
        """
        found = command.regexp_match
        if found is None:  # pragma: no cover — сюда не пускает фильтр
            return

        shown = await entry_view.full(int(found.group(1)))
        if shown is None:
            await message.answer(entry_view.NOT_FOUND)
            return

        # Длинная запись уходит несколькими сообщениями, а не обрезается:
        # обрезка здесь вернула бы ровно тот дефект, ради которого команда
        # и заведена (`vision.md` §8).
        for part in entry_view.parts(entry_view.text(shown, now=datetime.now(UTC))):
            await message.answer(part)

    @router.message(Command("recall"))
    async def recall(message: Message, command: CommandObject) -> None:
        """Срез по темам, а по названной теме — одна запись.

        Две команды в одной намеренно. Без темы это вопрос «о чём я думал»,
        с темой — «покажи». Разделять их на `/topics` и `/recall` значило бы
        просить владельца помнить две команды ради одного разговора.

        **Конкретная запись выдаётся только на спрос** (`vision.md` §5.4):
        срез называет темы и приглашает, а достаёт запись уже вторая команда.
        Тем и отличается от всплывания старого, которое приходит само и без
        повода.
        """
        now = datetime.now(UTC)
        days = settings.topics_period_days
        # Модель строится всегда, а зовётся только при `RECALL_WRITTEN=true`:
        # `build_model` в сеть не ходит, а ветвление здесь стоило бы `None`
        # в двух местах ниже. Так же собран воркер.
        writer = llm.build_model(settings, model=settings.digest_model)

        topic = (command.args or "").strip()
        if not topic:
            found = await digest.by_topic.collect(now, days=days)
            if found is None:
                await message.answer(NO_TOPICS)
                return
            await message.answer(await digest.by_topic.text(writer, found, days=days, now=now))
            return

        talk = await archive_topics.collect(topic, now)
        if talk is None:
            known = await digest.by_topic.names(now, days=days)
            listed = ", ".join(known) if known else "пока никаких"
            await message.answer(NO_SUCH_TOPIC.format(topic=topic, known=listed))
            return

        await message.answer(archive_topics.text(talk, timezone=settings.brain_timezone, now=now))

    @router.message(F.voice)
    async def voice(message: Message) -> None:
        ack = await message.answer(ACK)

        # Проверка режима — до всего остального: в приватном режиме файл
        # не скачивается и запись не создаётся, наружу не уходит ничего.
        if await repository.is_private_mode():
            await ack.edit_text(PRIVATE_VOICE_REFUSED)
            return

        if message.voice is None:  # pragma: no cover — исключено фильтром F.voice
            return

        incoming = ingest.IncomingVoice(
            chat_id=message.chat.id,
            message_id=message.message_id,
            ack_message_id=ack.message_id,
            file_id=message.voice.file_id,
            duration_sec=message.voice.duration,
            # Голосом отвечают чаще, чем текстом, — это основной вход системы.
            # Забыть связку здесь значило бы, что раскачка работает только
            # для набранного руками, ровно как напоминания работали только
            # для текста (дыра 16 августа).
            answers=await _answering(message),
        )

        try:
            async with capture_transaction() as uow:
                result = await ingest.capture_voice(
                    uow,
                    incoming,
                    is_private=False,
                    timezone=settings.brain_timezone,
                    audio_dir=audio_dir,
                )
        except Exception:
            logger.exception("голосовое не сохранено")
            await ack.edit_text(CAPTURE_FAILED)
            return

        if result.duplicate:
            # Тот же апдейт пришёл дважды. Записи не прибавилось — и уведомления
            # тоже не должно: владелец отправлял сообщение один раз.
            await ack.delete()
            return

        # Подтверждение остаётся как есть: его отредактирует воркер,
        # когда расшифровка будет готова.

    @router.message(F.text)
    async def text(message: Message) -> None:
        # Ответ на вопрос бота — это реплика диалога, а не новая мысль.
        # Проверяется до подтверждения приёма: «сегодня в 19:00» не должно
        # ни лечь в архив, ни получить «Записал.».
        if await _answered_a_question(message):
            return

        ack = await message.answer(ACK)

        if message.text is None:  # pragma: no cover — исключено фильтром F.text
            return

        incoming = ingest.IncomingText(
            chat_id=message.chat.id,
            message_id=message.message_id,
            ack_message_id=ack.message_id,
            text=message.text,
            answers=await _answering(message),
        )

        private = await repository.is_private_mode()

        try:
            async with capture_transaction() as uow:
                result = await ingest.capture_text(
                    uow,
                    incoming,
                    is_private=private,
                    timezone=settings.brain_timezone,
                    now=datetime.now(UTC),
                )
        except Exception:
            logger.exception("запись не сохранена")
            await ack.edit_text(CAPTURE_FAILED)
            return

        if result.duplicate:
            # Тот же апдейт пришёл дважды. Ни записи, ни ответа по архиву:
            # владелец спрашивал один раз.
            await ack.delete()
            return

        if result.ask is reminders.Ask.CONFIRM and result.entry_id is not None:
            # Время уже разобрано — остаётся подтвердить. Открытый вопрос
            # («когда напомнить?») кнопок не получает: ответ там свободный,
            # и кнопка под ним обещала бы не то.
            await ack.edit_text(result.reply, reply_markup=yes_no_keyboard(result.entry_id))
        else:
            # Явное «напомни в четверг в 19:00» заводит напоминание прямо здесь,
            # без вопроса, — и это самый частый случай владельца. Значит,
            # и предложение календаря живёт здесь тоже: одна ветка из двух
            # оставила бы кнопку невидимой ровно там, где она нужнее всего.
            await ack.edit_text(result.reply, reply_markup=await _calendar_offer(result.entry_id))

        # Ответ по архиву идёт **после** записи и **отдельным сообщением**.
        # См. `_from_archive`: это и есть то место, где решается, что делать
        # с ложным срабатыванием признака.
        await _from_archive(message, private=private)

    async def _from_archive(message: Message, *, private: bool) -> None:
        """Ответить по архиву на сообщение, которое выглядит вопросом к нему.

        **Запись при этом уже сохранена, и это главное решение здесь.** Признак
        «вопрос к архиву» разбирает код по конечному списку форм
        (`present/archive_question`), и ошибаться он будет в обе стороны.
        Цена ошибок разная, и потому они разведены:

            признак не сработал   владелец получил «Записал.» и не получил
                                  ответа — обидно, но мысль цела, и `/find`
                                  никуда не делся;
            признак сработал зря  владелец получил «Записал.» **и** лишнее
                                  сообщение с выдачей поиска — шум на один
                                  экран, и мысль всё равно цела.

        Другой порядок («это похоже на вопрос — значит, не записываю»)
        недопустим ни при каком качестве признака: `entries` только на вставку
        (инвариант 3), и не сохранённая мысль не восстановится ниоткуда —
        ни командой, ни из логов, ни из переписки. Поэтому поиск здесь
        **добавляется** к записи, а не заменяет её, и подтверждение «Записал.»
        владелец видит в любом случае.

        Порядок внутри тоже не случайный: сначала разговор по теме, потом
        поиск. Разговор не выходит за пределы базы и стоит ноль запросов
        к провайдеру, а поиск платит вектором запроса за каждый вопрос.
        """
        if not settings.archive_answers:
            # Выключатель есть у всего, что живёт на каждом сообщении
            # (`LINKS_ENABLED`, `CLEANUP_ENABLED`): снимать такой механизм
            # надо строкой в `.env`, а не правкой кода. Молчание тут полное —
            # запись при этом сохраняется, она сохранилась выше по коду.
            return

        question = archive_question.asked(
            message.text or "", mark_limit=settings.archive_question_mark_limit
        )
        if question is None:
            return

        # Срабатывание механизма, живущего на каждом сообщении, обязано быть
        # видно в журнале: иначе «почему он вдруг ответил поиском» не разобрать.
        logger.info("вопрос к архиву (%s): %r", question.signal, question.query)
        now = datetime.now(UTC)

        try:
            talk = await archive_topics.collect(question.query, now)
            if talk is not None:
                await message.answer(
                    archive_topics.text(talk, timezone=settings.brain_timezone, now=now)
                )
                return

            # Вторая дверь ведёт себя ровно как первая (`/find`), и это
            # решение владельца 07.09.2026: до него она в приватном режиме
            # не искала вовсе, а команда искала и ходила наружу.
            # Довод дословно: «несогласованность двух дверей хуже любой
            # из них — привыкнет к одной, ошибётся на другой».
            found = (
                await search.local(question.query, limit=RESULTS)
                if private
                else await search.search(
                    embeddings.build_embedder(settings), question.query, limit=RESULTS
                )
            )
        except Exception:
            # Поиск ходит наружу за вектором запроса, и провайдер падает.
            # Молчать нельзя: владелец решит, что в архиве ничего нет.
            logger.exception("ответ по архиву не собрался: %r", question.query)
            await message.answer(SEARCH_FAILED)
            return

        await message.answer(search_text.render(question.query, found, today=now, local=private))

    async def _answered_a_question(message: Message) -> bool:
        """Разобрать ответ на «когда напомнить?». `False` — это не ответ.

        Запись ищется по сообщению, на которое отвечают: вопрос — это
        отредактированное подтверждение приёма, и его идентификатор лежит
        в `entries.meta`. Поэтому ни таблицы ожидающих вопросов, ни состояния
        в памяти процесса не нужно.
        """
        replied_to = message.reply_to_message
        if replied_to is None or message.text is None:
            return False

        found = await repository.entry_by_ack_message(message.chat.id, replied_to.message_id)
        if found is None:
            return False

        entry_id, subject = found
        if await repository.has_reminder(entry_id):
            return False

        async with capture_transaction() as uow:
            outcome = await reminders.schedule_confirmed(
                uow,
                entry_id=entry_id,
                chat_id=message.chat.id,
                subject=subject,
                when=message.text,
                timezone=settings.brain_timezone,
                now=datetime.now(UTC),
            )

        await replied_to.edit_text(_answered(outcome), reply_markup=await _calendar_offer(entry_id))
        return True

    @router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
    async def answer_question(callback: CallbackQuery) -> None:
        """Нажатие «Да» или «Нет» под вопросом «это напоминание?»."""
        await callback.answer()

        if callback.data is None or callback.message is None:  # pragma: no cover — фильтр выше
            return

        _, answer, raw_entry_id = callback.data.split(":", maxsplit=2)
        entry_id = int(raw_entry_id)
        question = callback.message

        if not isinstance(question, Message):  # pragma: no cover — слишком старое сообщение
            return

        if answer == NO:
            await question.edit_text(STORED)
            return

        # Нажать кнопку можно сколько угодно раз: она живёт в переписке вечно.
        # Второе напоминание об одном и том же раздражает сильнее, чем его
        # отсутствие, — та же причина, что и у повтора задания расшифровки.
        if await repository.has_reminder(entry_id):
            await question.edit_text(ALREADY_SCHEDULED)
            return

        found = await repository.entry_by_ack_message(question.chat.id, question.message_id)
        if found is None:
            logger.error("ответ на вопрос без записи: entry_id=%s", entry_id)
            await question.edit_text(QUESTION_LOST)
            return

        _, subject = found
        async with capture_transaction() as uow:
            outcome = await reminders.schedule_confirmed(
                uow,
                entry_id=entry_id,
                chat_id=question.chat.id,
                # «Да» означает «время в записи верное» — оттуда его и берём.
                subject=subject,
                when=subject,
                timezone=settings.brain_timezone,
                now=datetime.now(UTC),
            )

        await question.edit_text(_answered(outcome), reply_markup=await _calendar_offer(entry_id))

    @router.callback_query(F.data.startswith(f"{CALENDAR_PREFIX}:"))
    async def add_to_calendar(callback: CallbackQuery) -> None:
        """Нажатие «в календарь». **Единственный путь события наружу.**

        Критерий готовности Фазы 5 — «ни одно событие не создано без моего
        нажатия» (`tech_plan.md` §8), и держится он на том, что
        `events.create_for_entry` зовётся отсюда и больше ниоткуда.

        Отказ здесь ничего не роняет: запись цела, напоминание внутри бота
        работает как раньше. Календарь — канал доставки, а не хранилище.
        """
        await callback.answer()

        if callback.data is None or callback.message is None:  # pragma: no cover — фильтр выше
            return

        message = callback.message
        if not isinstance(message, Message):  # pragma: no cover — слишком старое сообщение
            return

        entry_id = int(callback.data.split(":", maxsplit=2)[2])
        result = await calendar.create_for_entry(entry_id, now=datetime.now(UTC))

        # Кнопка снимается при любом исходе: она уже нажата, и второй раз она
        # либо ничего не даст, либо даст второе событие. Текст при этом
        # дописывается к прежнему, а не заменяет его — владелец должен видеть,
        # на что именно он нажал.
        await message.edit_text(
            f"{message.text}\n\n{_calendar_said(result, settings)}", reply_markup=None
        )

    @router.callback_query(F.data.startswith(f"{ANNOTATION_PREFIX}:"))
    async def annotation_button(callback: CallbackQuery) -> None:
        """Нажатия под готовым разбором: «не туда», выбор типа, «приватная»."""
        await callback.answer()

        if callback.data is None or callback.message is None:  # pragma: no cover — фильтр выше
            return

        message = callback.message
        if not isinstance(message, Message):  # pragma: no cover — слишком старое сообщение
            return

        _, action, *rest = callback.data.split(":")

        if action == WRONG:
            # Список типов вместо текущего разбора. Текст остаётся прежним —
            # выбирать тип, не видя записи, невозможно.
            await message.edit_reply_markup(
                reply_markup=notifier_module.type_keyboard(int(rest[0]), enrich.NAMES)
            )
            return

        if action == SET:
            type_value, entry_id = rest[0], int(rest[1])
            chosen = AnnotationType(type_value)
            was = await overrides.reclassify(entry_id, to=chosen)
            await message.edit_text(_reclassified(chosen, was), reply_markup=None)
            return

        if action == PRIVATE:
            result = await overrides.make_private(int(rest[0]))
            await message.edit_text(_made_private(result), reply_markup=None)

    @router.callback_query(F.data.startswith(f"{digest_keyboards.DEED_PREFIX}:"))
    async def deed_button(callback: CallbackQuery) -> None:
        """Кнопки под делом в утренней сводке — все три уровня и отмена.

        **Текст сводки не переписывается**: в ней несколько дел, и стирать весь
        список из-за одного нажатия нельзя. Меняются только кнопки, и только
        у того дела, на которое нажали.

        **Ни одного вызова модели на нажатие.** Перерисовка идёт из того, что
        пришло вместе с нажатием: Telegram присылает сообщение вместе с его
        кнопками, а в кнопках лежат идентификаторы дел и их порядок. Своей
        таблицы ожидающих вопросов в проекте нет и здесь не заводится.
        """
        if callback.data is None:  # pragma: no cover — фильтр выше
            return

        _, action, *rest = callback.data.split(":")
        commitment_id = int(rest[-1])
        message = callback.message if isinstance(callback.message, Message) else None

        if action == digest_keyboards.SEEN:
            # Подпись состояния — надпись, а не кнопка: рядов без кнопок
            # в Telegram не бывает. Нажатие на неё ничего не делает нарочно.
            await callback.answer()
            return

        if action == digest_keyboards.PICK:
            await _redraw(message, commitment_id, digest_keyboards.choice_rows(commitment_id))
            await callback.answer()
            return

        if action == digest_keyboards.SNOOZE:
            await _redraw(message, commitment_id, digest_keyboards.term_rows(commitment_id))
            await callback.answer()
            return

        if action == digest_keyboards.COLLAPSE:
            await _fold(message, commitment_id)
            await callback.answer()
            return

        now = datetime.now(UTC)
        if action == digest_keyboards.UNDO:
            # Возврат — единственное место, где состояние приходит снаружи:
            # прежнее по данным уже не восстановить, и оно едет в кнопке.
            # `return_target` прогоняется ещё раз, потому что значение съездило
            # в Telegram и обратно, а «отложено» без срока прячет дело навсегда.
            await digest.commitments.set_state(
                commitment_id,
                digest.commitments.return_target(CommitmentState(rest[0])),
                now=now,
            )
            await _fold(message, commitment_id)
            await callback.answer(RETURNED)
            return

        change = _deed_change(action, rest, now=now, timezone=settings.brain_timezone)
        if change is None:
            # Незнакомое нажатие — наша же ошибка в грамматике кнопок. Молча
            # закрыть дело было бы хуже всего: владелец не просил.
            logger.error("нажатие под делом с непонятным действием: %s", callback.data)
            await callback.answer()
            return

        state, until = change
        previous = await digest.commitments.set_state(commitment_id, state, now=now, until=until)
        if previous is None:
            await callback.answer(DEED_LOST)
            return

        await _redraw(message, commitment_id, _rows_after(commitment_id, state, previous))
        await callback.answer(digest_keyboards.STATE_LABELS[state] + ".")

    @router.callback_query(F.data.startswith(f"{digest_keyboards.LOOP_PREFIX}:"))
    async def loop_button(callback: CallbackQuery) -> None:
        """Кнопки петли: заведение под разбором и ответы в утренней сводке."""
        if callback.data is None or callback.message is None:  # pragma: no cover — фильтр выше
            return

        _, action, raw_id = callback.data.split(":", maxsplit=2)
        identifier = int(raw_id)

        if action == digest_keyboards.NEW:
            # Здесь `raw_id` — это запись, а не петля: кнопка живёт под разбором.
            created = await digest.loops.create_from_entry(identifier)
            await callback.answer(LOOP_CREATED if created else LOOP_NOT_FOUND, show_alert=True)
            return

        await callback.answer(await _answer_loop(action, identifier))

    @router.callback_query(F.data.startswith(f"{digest_keyboards.HABIT_PREFIX}:"))
    async def habit_button(callback: CallbackQuery) -> None:
        """Кнопки привычки под разбором: заведение и выбор ритма.

        Три нажатия, и все три про одну запись — номер в `callback_data` всегда
        её, а не привычки: привычки в этот момент может ещё не быть.

            habit:new:17      «Привычка» — завести, если ритм понятен из слов
            habit:other:17    «Свой вариант» — развернуть второй ряд ритмов
            habit:daily:17    выбранный ритм

        **Ритм спрашивается, а не угадывается** (`vision.md` §5.11): разобранный
        из слов записи ритм заводит привычку сразу, а неразобранный превращает
        кнопки в вопрос. Умолчания нет намеренно — угаданный ритм это ежедневное
        сообщение, выбранное за владельца.
        """
        if callback.data is None:  # pragma: no cover — фильтр выше
            return

        _, action, raw_id = callback.data.split(":", maxsplit=2)
        entry_id = int(raw_id)
        message = callback.message if isinstance(callback.message, Message) else None

        offered = await digest.habits.proposal(entry_id)
        if offered is None:
            # Привычку либо уже завели этой же кнопкой, либо запись переразметили
            # между показом и нажатием: кнопка живёт в переписке вечно,
            # а разметка — нет.
            await callback.answer(HABIT_NOT_FOUND, show_alert=True)
            return

        if action == digest_keyboards.OTHER:
            await _ask_rhythm(message, entry_id, other=True)
            await callback.answer()
            return

        chosen = None if action == digest_keyboards.NEW else Rhythm(action)
        created = await digest.habits.confirm(
            offered, chosen=chosen, now=datetime.now(UTC), timezone=settings.brain_timezone
        )
        if created is None:
            # Ритма нет ни в словах, ни в нажатии — значит спрашиваем.
            await _ask_rhythm(message, entry_id, other=False)
            await callback.answer()
            return

        if message is not None:
            with suppress(TelegramBadRequest):
                await message.edit_reply_markup(reply_markup=None)
        await callback.answer(_habit_created(offered, chosen), show_alert=True)

    @router.callback_query(F.data.startswith(f"{digest_keyboards.EVENING_PREFIX}:"))
    async def evening_button(callback: CallbackQuery) -> None:
        """Кнопки вечернего разбора: «ок», «не туда» и выбор типа."""
        if callback.data is None or callback.message is None:  # pragma: no cover — фильтр выше
            return

        message = callback.message
        parts = callback.data.split(":")
        action = parts[1]

        if action == digest_keyboards.OK:
            await digest.evening.settle(int(parts[2]), action=OverrideAction.CONFIRM)
            await callback.answer(SETTLED)
            return

        if action == digest_keyboards.WRONG:
            # Список типов вместо кнопок пачки. Текст сообщения остаётся:
            # выбирать тип, не видя записи, невозможно.
            if isinstance(message, Message):
                await message.edit_reply_markup(
                    reply_markup=digest_keyboards.evening_types_keyboard(
                        int(parts[2]), enrich.NAMES
                    )
                )
            await callback.answer()
            return

        if action == digest_keyboards.SET:
            chosen = AnnotationType(parts[2])
            await overrides.reclassify(int(parts[3]), to=chosen)
            await callback.answer(f"📌 {enrich.NAMES[chosen]}")

    return router


async def _answering(message: Message) -> dict[str, object] | None:
    """На какой вопрос раскачки отвечает это сообщение. `None` — ни на какой.

    Ответ на вопрос бота обязан лечь **записью архива**, а не пройти мимо: без
    этого раскачка даёт повод сказать и теряет сказанное, то есть делает
    половину дела (план Фазы 4, блок A, пункт 6). Отличие от ответа на «когда
    напомнить?» здесь принципиальное: там реплика диалога, и записью она
    не становится; здесь запись и есть смысл вопроса.

    Связка держится на `reply_to_message` — том же приёме, что у напоминаний.
    Своей таблицы ожидающих вопросов в проекте нет: состояние вопроса живёт
    в самой переписке, и перезапуск бота его не ломает.
    """
    replied_to = message.reply_to_message
    if replied_to is None:
        return None
    found = await digest.prompting.answered(replied_to.message_id)
    return None if found is None else found.as_meta()


RETURNED = "Вернул."
DEED_LOST = "Не нашёл этого дела — возможно, запись переразметили."
SETTLED = "Принял."
HABIT_NOT_FOUND = "Не нашёл здесь привычки — возможно, она уже заведена или запись переразметили."


async def _ask_rhythm(message: Message | None, entry_id: int, *, other: bool) -> None:
    """Спросить ритм, заменив кнопки под тем же сообщением.

    Текст разбора остаётся на месте: выбирать ритм, не видя записи, — то же
    самое, что выбирать тип вслепую в «не туда».
    """
    if message is None:  # pragma: no cover — сообщение приходит вместе с нажатием
        return
    with suppress(TelegramBadRequest):
        await message.edit_reply_markup(
            reply_markup=digest_keyboards.rhythm_keyboard(entry_id, other=other)
        )


def _habit_created(offered: digest.habits.Proposal, chosen: Rhythm | None) -> str:
    """Что сказать после заведения. Ритм повторяется словами владельца.

    Повторяется намеренно: привычка начинает присылать сообщения сама, и
    единственный момент, когда владелец видит, **на что именно** он согласился,
    — этот. Формулировка берётся его: «два раза в неделю», а не «по
    понедельникам и четвергам», даже когда дни совпали (`recurrence.describe`).
    """
    found = offered.rhythm if chosen is None else recurrence.chosen(chosen)
    said = recurrence.describe(found) if found is not None else ""
    return f"🌱 Привычка: {said}. Спрошу в {offered.hour}:00, как продвижение."


LOOP_CREATED = "🔁 Завёл открытый вопрос. Буду возвращаться к нему."
LOOP_NOT_FOUND = "Не нашёл открытого вопроса в этой записи — возможно, её переразметили."
# Ответ повторяет глагол кнопки и остаётся безличным: «Закрыл» и «Убрал» —
# это система отчитывается за владельца в прошедшем времени, ровно то, из-за
# чего подписи и переписали.
LOOP_ANSWERS = {
    digest_keyboards.CLOSED: "🔁 Решено.",
    digest_keyboards.IRRELEVANT: "🔁 Отпало.",
    digest_keyboards.THINKING: "🔁 Вернусь через две недели.",
    digest_keyboards.REPHRASE: (
        "🔁 Убрал этот вопрос. Запишите мысль заново, как она звучит сейчас — "
        "новая формулировка получит свой разбор."
    ),
}


async def _answer_loop(action: str, loop_id: int) -> str:
    """Что сделать с петлёй и что сказать в ответ.

    «Переформулирую» закрывает петлю и просит записать мысль заново, а не
    открывает поле ввода. В этой системе способ что-то сказать — записать это:
    новая формулировка, пришедшая обычной записью, получит разметку, связи
    и своё подтверждение петли, а введённая в ответ на кнопку не получила бы
    ничего из этого.
    """
    if action == digest_keyboards.THINKING:
        await digest.loops.still_thinking(loop_id, now=datetime.now(UTC))
    elif action == digest_keyboards.IRRELEVANT:
        await digest.loops.settle(loop_id, LoopStatus.IRRELEVANT)
    else:
        await digest.loops.settle(loop_id, LoopStatus.CLOSED)
    return LOOP_ANSWERS.get(action, "🔁 Готово.")


# Во что переводит нажатие второго уровня. Словарём, а не цепочкой `if` с
# «остальное — завершить»: у незнакомого действия не должно быть состояния
# по умолчанию, и уж точно не «завершено».
DEED_STATES = {
    digest_keyboards.DONE: CommitmentState.DONE,
    digest_keyboards.WORK: CommitmentState.IN_PROGRESS,
    digest_keyboards.DROP: CommitmentState.DROPPED,
}


def _deed_change(
    action: str, rest: list[str], *, now: datetime, timezone: str
) -> tuple[CommitmentState, datetime | None] | None:
    """Во что переводит нажатие и с каким сроком. `None` — нажатие непонятное.

    Срок берётся отсюда, а не из ядра, по одной причине: он зависит от таймзоны
    владельца, а её знает настройка бота. Сам расчёт — в ядре
    (`digest.commitments.snooze_term`), потому что «До завтра» означает
    «к завтрашней сводке», и это правило замысла, а не оформления.
    """
    if action == digest_keyboards.UNTIL and rest[0] in digest.commitments.TERMS:
        return CommitmentState.SNOOZED, digest.commitments.snooze_term(
            now, term=rest[0], timezone=timezone
        )
    state = DEED_STATES.get(action)
    return None if state is None else (state, None)


def _rows_after(
    commitment_id: int, state: CommitmentState, previous: CommitmentState
) -> list[list[InlineKeyboardButton]]:
    """Что показать под делом после перехода.

    Три состояния из четырёх убирают дело из сводки — после них действовать
    больше не над чем, и подтверждение с отменой на своём месте.

    `in_progress` **оставляет дело на месте**, и в этом вся разница: пометил
    «в работе», через час сделал — а завершить нечем до завтрашней сводки,
    потому что ряд свернулся в подтверждение. Поэтому выбор остаётся открытым.
    Отмена там не нужна вовсе: дело видно в сводке, состояние видно значком,
    и поменять его можно в любой момент теми же кнопками.
    """
    if state is CommitmentState.IN_PROGRESS:
        return digest_keyboards.choice_rows(commitment_id)
    return [
        digest_keyboards.confirmed_row(
            commitment_id, state, back_to=digest.commitments.return_target(previous)
        )
    ]


async def _redraw(
    message: Message | None, commitment_id: int, rows: list[list[InlineKeyboardButton]]
) -> None:
    """Перерисовать ряды одного дела в сообщении сводки.

    Молча ничего не делает, если кнопок нет: сообщение может быть слишком
    старым, чтобы Telegram прислал его целиком. Состояние дела к этому моменту
    уже изменено — перерисовка косметическая, и ронять из-за неё нажатие
    не за что.
    """
    if message is None or message.reply_markup is None:
        return
    with suppress(TelegramBadRequest):
        await message.edit_reply_markup(
            reply_markup=digest_keyboards.redraw(message.reply_markup, commitment_id, rows)
        )


async def _fold(message: Message | None, commitment_id: int) -> None:
    """Свернуть дело обратно в номер — после «Назад» и после отмены.

    Номер берётся из самой клавиатуры: порядок рядов совпадает с нумерацией
    в тексте сводки, а текст мы после отправки не перечитываем.
    """
    if message is None or message.reply_markup is None:
        return
    number = digest_keyboards.deed_numbers(message.reply_markup).get(commitment_id)
    if number is None:  # pragma: no cover — кнопка не из этой сводки
        return
    await _redraw(message, commitment_id, [digest_keyboards.deed_row(number, commitment_id)])


def _answered(outcome: reminders.ScheduleOutcome) -> str:
    """Что показать вместо вопроса, когда на него ответили."""
    return f"⏰ {outcome.message}" if outcome.created else outcome.message


async def _calendar_offer(entry_id: int | None) -> InlineKeyboardMarkup | None:
    """Предложить положить это же в календарь. `None` — не предлагать.

    Зовётся из **обоих** мест, где у записи появляется напоминание: явное
    «напомни в четверг в 19:00» заводит его сразу при приёме, а повелительный
    глагол без «напомни» — только после нажатия «Да». Второе место без первого
    оставило бы самый частый случай владельца без кнопки вовсе.

    Все условия — в `events.offered`, и здесь их намеренно не дублируется
    ни одного: календарь настроен, напоминание есть, запись не приватная,
    события по ней ещё нет.

    Здесь же проходит граница фазы: кнопка предлагает, событие создаёт нажатие.
    Между этими двумя моментами владелец может передумать, и система обязана
    это пережить — поэтому приватность проверяется ещё раз, уже в `integrations`.
    """
    if entry_id is None:
        return None
    if not await calendar.offered(entry_id):
        return None
    return calendar_keyboard(entry_id)


def _calendar_said(result: calendar.Result, settings: Settings) -> str:
    """Что показать после нажатия «в календарь».

    Владелец видит **текст, время и имя календаря** — это требование приёмки
    Фазы 5, а не вежливость: «молча события не создаются» проверяется тем,
    что после нажатия он читает, что именно ушло и куда.

    Время — в его поясе. В UTC оно было бы правдоподобным и неверным, а такая
    ошибка в сообщении про встречу дороже любой другой.
    """
    if result.outcome is calendar.Outcome.CREATED and result.starts_at is not None:
        local = result.starts_at.astimezone(ZoneInfo(settings.brain_timezone))
        return CALENDAR_ADDED.format(
            title=result.title, when=f"{local:%d.%m в %H:%M}", calendar=result.calendar
        )

    if result.outcome is calendar.Outcome.ALREADY:
        return CALENDAR_ALREADY
    if result.outcome is calendar.Outcome.PRIVATE:
        return CALENDAR_PRIVATE
    if result.outcome is calendar.Outcome.OFF:
        return CALENDAR_OFF
    if result.outcome is calendar.Outcome.NO_REMINDER:
        return CALENDAR_NO_REMINDER
    return CALENDAR_FAILED.format(detail=result.detail)


def _reclassified(chosen: AnnotationType, was: AnnotationType | None) -> str:
    """Что показать после поправки типа.

    Прежний тип называется вслух: поправка — это то, из чего потом считается
    качество разметки, и владелец должен видеть, что именно он поправил,
    а не только чем заменил.
    """
    name = enrich.NAMES[chosen]
    if was is None or was is chosen:
        return f"📌 {name}"
    return f"📌 {name} (было: {enrich.NAMES[was]})"


def _made_private(result: overrides.MadePrivate) -> str:
    """Что показать после «сделать приватной».

    Главное здесь — вторая строка. ADR-0002 разрешает ретроактивную приватность
    ровно с условием: бот обязан сказать, что текст уже уходил наружу, а не
    делать вид, что всё вернулось как было. Отозвать это нельзя, и молчать
    об этом — значит обещать приватность, которой нет.
    """
    lines = ["🔒 Записана как приватная. Наружу больше не уйдёт."]
    if result.went_outside:
        lines.append(
            "Но текст уже уходил во внешнюю модель раньше — этого не отозвать. "
            f"Убрал: векторов {result.cleared_vectors}, разметок {result.removed_annotations}."
        )
    return "\n\n".join(lines)
