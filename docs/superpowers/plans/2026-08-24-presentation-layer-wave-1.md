# Слой представления · Волна 1 — план реализации

> **Для агентов:** обязательный под-навык — `superpowers:subagent-driven-development`
> или `superpowers:executing-plans`. Шаги помечены чекбоксами.

**Цель:** владелец нигде не видит обрывков собственной речи, а утреннюю сводку
пишет модель — из данных, отобранных кодом.

**Архитектура:** новый слой ядра `core/present/` пишет текст, который увидит
человек, и не делает больше ничего. Отбор, пороги, бюджет, расписание, порядок
и нумерация остаются кодом. Наружу слой ходит через существующую дверь
`core/llm`, поэтому инвариант 5, учёт в `llm_calls` и маршрутизация тира
достаются конструкцией. Ответ модели проверяется перед показом и при любом
расхождении заменяется прежним сухим рендером.

**Стек:** Python 3.13 · SQLAlchemy 2.0.52 + Alembic 1.19.1 · pgqueuer 1.3.2 ·
aiogram 3.30 · pytest 9.1.1 · mypy strict · ruff 0.16.3.

**Решение:** [`docs/adr/0015-presentation-layer.md`](../../adr/0015-presentation-layer.md).
Разбор, из которого волна выросла: [`docs/revision_after_use.md`](../../revision_after_use.md).

## Состояние на 26 августа 2026 · код закрыт, ждёт приёмки

**Все десять задач сделаны, кроме одной, выброшенной решением владельца.
Волна при этом не закрыта:** её критерий — «сводка читается за сорок секунд
и после неё хочется открыть бота завтра» — проверяется глазами владельца,
а он сводку **не видел ни разу**. Приёмка и порядок завтрашнего дня —
`docs/status.md`, раздел «С чего продолжать».

| задача | состояние |
|---|---|
| 1 · два текста | закрыта |
| 2 · чистка расшифровки | код закрыт, **на живой записи не работала ни разу** |
| 3 · замер чистки | закрыт первый уровень; второй (разметка владельца) не сделан |
| 4 · названия дел и петель | код закрыт, **ни одного названия не посчитано** — квота |
| 5 · починка петли | закрыта; заодно согласован текст вопроса с кнопками |
| 6 · гвард | закрыт |
| 7 · сводка через модель | код закрыт, **лид не написан ни разу** |
| 8 · убрать упрёк | закрыта |
| 9 · четыре состояния и кнопки | закрыта; подписи кнопок переписаны, добавлена отмена последнего действия |
| 10 · документы и приёмка | этот блок и разделы `status.md` |
| ~~все дела страницами~~ | **выброшена владельцем 26.08.2026**: список — симптом. Настоящий вопрос — почему ничто не убирает дела само. Наброски (угасание · поиск · срез по темам) — `docs/status.md`, «Дела накапливаются» |

**Появилось сверх плана:** починка двери `core/llm` под Gemini 3.x (уровень
рассуждения, время у упавшего вызова, проба при старте с рабочей схемой
и учётом), паузы замера под поминутный лимит, расход квоты разбивкой
по моделям, `--preview` у бэкфилла.

### Решения, принятые по ходу и записанные, чтобы не читались как недоделки

**У петли нет отмены последнего действия, и это решение.** Из трёх её кнопок
под общий механизм ложатся две: `Решено` и `Отпало` меняют состояние. Третья,
`Отложить`, состояния **не меняет вовсе** — она увеличивает счётчик и двигает
дату; отменить её значит вернуть прежнюю дату, а её нет ни в кнопке, ни
в данных. Ряд, где две кнопки отменяются, а третья молча нет, хуже ряда
без отмены совсем.

**`in_progress` не сворачивается в подтверждение**, в отличие от трёх
остальных переходов. Причина: `done`, `snoozed` и `dropped` убирают дело
из сводки — после них действовать больше не над чем. `in_progress` оставляет
дело на месте, и свёрнутый ряд означал бы «пометил в работе — до завтра
не завершить».

**Счётчик откладываний петли не уходит в модель.** Не потому, что не влезает,
а потому что упрёк по счётчику гвард поймать не может в принципе: он ловит
лексику. Не зная счётчика, модель не может им попрекнуть — упрёк становится
**невозможен**, а не отлавливается постфактум.

## Общие ограничения

Действуют на **каждую** задачу плана.

- **Коммиты не делаются.** Их делает владелец, руками, прочитав дифф
  (`CLAUDE.md`, «Чего нельзя»). Шаг «Commit» в этом плане означает: остановиться,
  показать дифф, дождаться владельца.
- **Тест обязан падать на текущем коде.** Шаг «прогнать и убедиться, что красный»
  не пропускается никогда: зелёный с первого раза означает, что тест проверяет
  не то.
- **Данные для тестов — из базы, а не из головы.** Живые строки берутся
  `tests/archive.py` **по номеру записи**; архив в git не уходит (ADR-0002).
- **Замер воспроизводит рабочий режим.** Воркер чистит по одной записи — значит
  и замер по одной. В шапке каждого прогона пишется, какой режим он повторяет.
- **Русские комментарии, объясняющие «почему».** Типы везде, mypy strict.
- **Заголовок коммита — английский**, тело — русское. Conventional Commits.
- **Ни одной новой зависимости.** Всё делается на том, что уже в проекте.
- **Приватная запись наружу не уходит** (инвариант 5). Каждый новый вызов идёт
  через `llm.complete_over_entries`, которая это проверяет.
- **Живые вызовы Gemini из сессии агента невозможны** (гео-блокировка). Всё, что
  требует настоящего вызова, готовится и передаётся владельцу командой.
- Не заканчивать сессию с красными тестами: `just check` обязан быть зелёным.

## Что уже посчитано, чтобы не считать заново

| | |
|---|---|
| символов на токен, русский | **2.9** (замер по 152 вызовам в `llm_calls`) |
| темп | 9.4 записи в день, 8 голосовых |
| средняя расшифровка | 544 символа |
| открытых дел / петель | 70 / 1 |
| стоимость волны | $0.38 в месяц по живому темпу, $0.78 по плановому |

## Карта файлов

| файл | ответственность |
|---|---|
| `core/present/__init__.py` | граница слоя: `cosmetic_allowed()`, общие константы, `PURPOSES` |
| `core/present/cleanup.py` | вычищенная расшифровка: промпт, схема, разбор |
| `core/present/titles.py` | названия дел и петель |
| `core/present/digest.py` | лид и строки утренней сводки |
| `core/present/guard.py` | проверка ответа модели: идентификаторы, цифры, длины, упрёк |
| `core/memory/repository.py` | **правится:** `display_text()` и `working_text()` вместо одного выражения |
| `core/digest/render.py` | **правится:** сборка сводки из готовых строк, сухой рендер как запасной |
| `adapters/telegram/digest_keyboards.py` | **правится:** кнопки четырёх состояний, «все дела» |

---

## Задача 1 · Два текста вместо одного

Фундамент всей волны: пока нет места под вычищенный текст и нет разделения
«показ против работы», ни одна следующая задача не имеет смысла.

**Файлы:**
- Создать: `src/brain/storage/migrations/versions/20260824_XXXX_clean_transcripts.py`
- Изменить: `src/brain/storage/models/transcript.py`
- Изменить: `src/brain/core/memory/repository.py:175-208`
- Изменить: `src/brain/config.py` (настройка `present_working_text`)
- Изменить (перевод на нужное выражение): `core/enrich/repository.py:65,97,319,395,576`,
  `core/retrieval/repository.py:86`, `core/digest/evening.py:102`,
  `core/digest/loops.py:119`, `core/memory/repository.py:132,161`,
  `maintenance.py:220`
- Тест: `tests/test_present_text_split.py`

**Интерфейсы:**
- Отдаёт наружу:
  - `memory.repository.display_text() -> Any` — SQL-выражение
    `coalesce(clean_text последней расшифровки, text последней расшифровки, raw_text)`
  - `memory.repository.working_text() -> Any` — то же, но `clean_text` участвует
    только при `PRESENT_WORKING_TEXT=clean`
  - `memory.repository.latest_clean_transcript() -> Any`
  - `Transcript.clean_text: Mapped[str | None]`
  - `Settings.present_working_text: Literal["verbatim", "clean"]`

**Куда что идёт** — классификация всех девяти мест, сверенная по коду:

| место | что там | выражение |
|---|---|---|
| `memory/repository.py:132` `entry_text` | вход эмбеддингов и чанков | `working_text()` |
| `memory/repository.py:161` `entry_by_ack_message` | разбор ответа «когда напомнить» | `working_text()` |
| `enrich/repository.py:65,97` | пачка на разметку | `working_text()` |
| `enrich/repository.py:319` | текст для ответа владельцу | **`display_text()`** |
| `enrich/repository.py:395` | пачка переобобщения тем | `working_text()` |
| `enrich/repository.py:576` | счётчик неразмеченного | `working_text()` |
| `retrieval/repository.py:86` | кандидаты на переиндексацию | `working_text()` |
| `digest/evening.py:102` | спорные записи в вечернем разборе | **`display_text()`** |
| `digest/loops.py:119` | нарезка вопроса петли по отрезку | `working_text()` |
| `maintenance.py:220` | нарезка дел по отрезку | `working_text()` |
| `digest/resurface.py:82-89` | всплывшая запись | **`display_text()`** |

Правило, по которому классифицировано: **всё, что режется по отрезку
(`span_start`/`span_end`), обязано брать `working_text()`** — отрезки посчитаны
над ним. Всё, что целиком уходит на экран, берёт `display_text()`.

- [x] **Шаг 1: тест, который падает на текущем коде**

```python
"""Показ берёт вычищенный текст, работа — дословный.

Разделение заведено ADR-0015: если чистка что-то потеряет, а поиск и отрезки
стоят на вычищенном, потерянное исчезнет молча и навсегда — оно останется
в дословном варианте и не найдётся. Поэтому рабочий текст переключается
настройкой и только после замера.
"""

import pytest
from sqlalchemy import select

from brain.core.memory.repository import display_text, working_text
from brain.storage.models.entry import Entry

pytestmark = pytest.mark.integration


async def test_display_prefers_clean_text(session, voice_entry_with_transcript):
    entry_id = voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )
    shown = await session.scalar(select(display_text()).where(Entry.id == entry_id))
    assert shown == "Надо посмотреть фильм."


async def test_working_stays_verbatim_by_default(session, voice_entry_with_transcript):
    entry_id = voice_entry_with_transcript(
        verbatim="э ну надо мне посмотреть фильм",
        clean="Надо посмотреть фильм.",
    )
    used = await session.scalar(select(working_text()).where(Entry.id == entry_id))
    assert used == "э ну надо мне посмотреть фильм"


async def test_display_falls_back_when_cleanup_has_not_run(session, voice_entry_with_transcript):
    """Чистка отстала — показывается дословный, а не пустота."""
    entry_id = voice_entry_with_transcript(verbatim="надо посмотреть фильм", clean=None)
    shown = await session.scalar(select(display_text()).where(Entry.id == entry_id))
    assert shown == "надо посмотреть фильм"
```

- [x] **Шаг 2: убедиться, что красный**

Прогнать: `uv run pytest tests/test_present_text_split.py -v`
Ожидается: `ImportError: cannot import name 'display_text'`.

- [x] **Шаг 3: миграция**

```python
def upgrade() -> None:
    # Вычищенная версия лежит рядом с дословной, а не вместо неё: инвариант
    # «сырое неизменяемо» про то, чтобы ничего не потерять, — не про то,
    # чтобы показывать сырое (ADR-0015, ось 2).
    op.add_column("transcripts", sa.Column("clean_text", sa.Text(), nullable=True))
    # Чем вычищено — отдельно от того, чем расшифровано: это разные вызовы
    # и, по замыслу, разные модели.
    op.add_column("transcripts", sa.Column("clean_model", sa.Text(), nullable=True))
    op.add_column("transcripts", sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("transcripts", "cleaned_at")
    op.drop_column("transcripts", "clean_model")
    op.drop_column("transcripts", "clean_text")
```

- [x] **Шаг 4: модель и два выражения**

В `storage/models/transcript.py` — три колонки с объяснением, почему
вычищенный текст не заменяет дословный.

В `core/memory/repository.py`:

```python
def latest_clean_transcript() -> Any:
    """Вычищенный текст последней расшифровки. NULL, пока чистка не прошла."""
    return (
        select(Transcript.clean_text)
        .where(Transcript.source_entry_id == Entry.id)
        .order_by(Transcript.id.desc())
        .limit(1)
        .scalar_subquery()
    )


def display_text() -> Any:
    """Текст, который видит владелец. Вычищенный, если он есть.

    Единственное место, где решается вопрос «что показать», — как
    `is_private_now()` для приватности. Второе такое выражение рядом однажды
    разъедется с этим, и разъедется молча.
    """
    return func.coalesce(latest_clean_transcript(), latest_transcript(), Entry.raw_text)


def working_text() -> Any:
    """Текст, с которым работает система: разметка, отрезки, чанки, индекс.

    Пока дословный. Переключение на вычищенный — настройка
    `PRESENT_WORKING_TEXT`, и включается она **после** замера чистки,
    а не вместе с ней (ADR-0015, ось 3).

    Переключив, обязательно прогнать `just reindex` и `just reenrich`:
    иначе половина архива останется посчитанной по прежнему тексту, и это
    не будет видно ниоткуда.
    """
    if get_settings().present_working_text == "clean":
        return func.coalesce(latest_clean_transcript(), latest_transcript(), Entry.raw_text)
    return func.coalesce(latest_transcript(), Entry.raw_text)
```

- [x] **Шаг 5: развести девять мест по таблице выше**

Каждое — заменой выражения. В `enrich/repository.py:319` и `digest/evening.py:102`
рядом ставится комментарий, почему именно там показ, а не работа.

- [x] **Шаг 6: зелёный и полная проверка**

Прогнать: `uv run pytest tests/test_present_text_split.py -v` → PASS.
Прогнать: `just check` и `just test-all` → зелено.

- [x] **Шаг 7: прогон по живой базе**

```bash
just migrate
uv run python -c "
import asyncio; from sqlalchemy import select, func
from brain.storage.db import session_scope
from brain.core.memory.repository import display_text, working_text
from brain.storage.models.entry import Entry
async def m():
    async with session_scope() as s:
        same = await s.scalar(select(func.count()).select_from(Entry).where(display_text() == working_text()))
        total = await s.scalar(select(func.count()).select_from(Entry))
        print(f'совпадают у {same} из {total} — до чистки обязано быть у всех')
asyncio.run(m())"
```

Ожидается: совпадают у всех. Это и есть доказательство, что задача 1 ничего
не изменила для владельца — она только развела дороги.

- [x] **Шаг 8: показать дифф владельцу**

```
feat(memory): split display text from working text
```

---

## Задача 2 · Чистка расшифровки

**Файлы:**
- Создать: `src/brain/core/present/__init__.py`, `src/brain/core/present/cleanup.py`
- Изменить: `src/brain/core/scheduler/queue.py` (задание `CLEANUP`)
- Изменить: `src/brain/core/scheduler/jobs.py` (постановка в транзакции, ожидание, показ)
- Изменить: `src/brain/worker.py` (обработчик, проверка модели при старте)
- Изменить: `src/brain/config.py`, `.env.example`
- Изменить: `src/brain/core/status.py` (строка «расшифровок без чистки»)
- Тест: `tests/test_present_cleanup.py`

**Интерфейсы:**
- Потребляет: `display_text()` из задачи 1
- Отдаёт наружу:
  - `present.cleanup.clean_one(model, *, entry_id, text) -> str | None`
  - `present.cleanup.PROMPT: str`, `present.cleanup.PURPOSE = "cleanup"`
  - `present.cosmetic_allowed(now) -> bool`
  - `queue.CLEANUP = "cleanup-transcript"`
  - `Settings.cleanup_model`, `Settings.cleanup_wait_sec`, `Settings.cleanup_enabled`

- [x] **Шаг 1: тест границы чистки — падает на текущем коде**

```python
"""Чистка убирает шум произнесения и не трогает смысл.

Проверяется на разборе ответа, а не на живой модели: живой вызов меряется
`just eval-cleanup`, и это работа владельца — из сессии агента провайдер
недоступен. Здесь проверяется наш код вокруг вызова.
"""

from brain.core.present import cleanup


def test_prompt_forbids_improving_before_it_allows_cleaning():
    """«Лучше недочистить» стоит в промпте раньше списка того, что убирать.

    Порядок не косметический: модель по умолчанию склонна улучшать текст,
    и правило, стоящее после списка задач, читается как оговорка.
    """
    text = cleanup.PROMPT
    assert "лучше недочистить" in text.lower()
    assert text.lower().index("лучше недочистить") < text.lower().index("слова-паразиты")
    for forbidden in ("не перефразируй", "не меняй порядок", "не исправляй согласования"):
        assert forbidden in text.lower()


def test_parse_takes_clean_field():
    parsed = cleanup.parse({"items": [{"id": 7, "clean": "Надо посмотреть фильм."}]}, entry_id=7)
    assert parsed == "Надо посмотреть фильм."


def test_parse_refuses_foreign_id():
    """Модель вернула чужую запись — берём ничего, а не чужой текст.

    Та же защита, что в разметке: в пачке идентификаторы идут подряд,
    и ошибиться на единицу легко.
    """
    assert cleanup.parse({"items": [{"id": 8, "clean": "чужое"}]}, entry_id=7) is None


def test_parse_refuses_empty():
    """Пустая чистка — отказ, а не успех: показывать было бы нечего."""
    assert cleanup.parse({"items": [{"id": 7, "clean": "   "}]}, entry_id=7) is None
```

- [x] **Шаг 2: убедиться, что красный**

Прогнать: `uv run pytest tests/test_present_cleanup.py -v`
Ожидается: `ModuleNotFoundError: brain.core.present`.

- [x] **Шаг 3: промпт и разбор**

```python
# Правило первой строкой — и это не оформление. Модель по умолчанию склонна
# улучшать текст: замер 19.08.2026 на разметке показал, что запрет, стоящий
# после задачи, читается как пожелание и обходится с обоснованием.
PROMPT = """Ты приводишь в читаемый вид расшифровку голосовой записи.

ГЛАВНОЕ ПРАВИЛО: лучше недочистить, чем перечистить. При любом сомнении
оставь как есть. Потеря смысла — дефект. Оставшееся «ну» — не дефект.

Убери: слова-паразиты («э», «мм», «ну вот», «это самое»), самоповторы
(«надо надо»), оборванные начала брошенных фраз. Расставь знаки препинания
и заглавные буквы.

Не трогай: смысл, порядок мыслей, лексику, оттенок, отрицания, числа, имена,
даты. Не перефразируй. Не меняй порядок слов. Не исправляй согласования
и падежи. Не дописывай ничего от себя. Слова «наверное», «кажется»,
«по-моему» — это степень уверенности, а не шум: оставь их.

Если чистить нечего, верни текст без изменений."""
```

`parse(data, *, entry_id)` возвращает `str | None`, отказываясь от чужого id
и от пустого текста. Температура 0.0 — разброс здесь только вредит, как
и в расшифровке.

- [x] **Шаг 4: зелёный**

Прогнать: `uv run pytest tests/test_present_cleanup.py -v` → PASS.

- [x] **Шаг 5: тест сквозного пути — падает**

```python
async def test_cleanup_job_stores_clean_text_and_edits_message(fake_model, notifier, archive):
    """Голосовое проходит путь до вычищенного текста в подтверждении приёма."""
    entry_id = archive.voice_entry(number=26)  # живая запись из базы, по номеру
    await jobs.cleanup(
        entry_id=entry_id, chat_id=1, ack_message_id=5, model=fake_model, notifier=notifier
    )

    stored = await repository.get_transcript_row(entry_id)
    assert stored.clean_text is not None
    assert stored.clean_model == fake_model.model
    assert notifier.edited[-1].text == stored.clean_text


async def test_private_entry_is_never_cleaned(fake_model, notifier, archive):
    """Приватность включена задним числом — чистка не идёт наружу (инвариант 5)."""
    entry_id = archive.voice_entry(number=26)
    await overrides.make_private(entry_id)
    await jobs.cleanup(
        entry_id=entry_id, chat_id=1, ack_message_id=5, model=fake_model, notifier=notifier
    )
    assert fake_model.calls == []
```

- [x] **Шаг 6: убедиться, что красный, затем провести задание**

Задание `CLEANUP` ставится **в той же транзакции**, что вставка расшифровки
(`jobs._store_transcript`, рядом с `INDEX` и `ENRICH`) — инвариант 7.
Обработчик в `worker.py` с `on_failure="hold"`, как остальные.

- [x] **Шаг 7: ожидание вместо мгновенного мусора**

```python
# Качество чистки важнее её скорости: мгновенно показанный мусор хуже
# небольшой задержки. Порог уточняется замером латентности в `just eval-cleanup`
# — до него это число из головы, и так оно и записано в долгах.
CLEANUP_WAIT_SEC = 30
```

Расшифровка, дождавшись чистки, редактирует подтверждение вычищенным текстом.
Не дождавшись — редактирует дословным, а задание чистки заменит его позже.

- [x] **Шаг 8: приоритет под квотой — по наблюдённому отказу**

Тест первым, и он обязан падать:

```python
async def test_cosmetics_stop_after_the_first_429(archive):
    """Придерживание включается с первого отказа, а не по настроенному числу.

    Лимит текстовой модели неизвестен и настройка пуста намеренно. Механизм,
    ждущий настроенного числа, при пустой настройке молчал бы ровно в тот день,
    ради которого написан, — и выглядел бы при этом работающим.
    """
    assert await present.cosmetic_allowed(now) is True
    archive.llm_call_failed(status=429, purpose="enrich", at=now - timedelta(hours=1))
    assert await present.cosmetic_allowed(now) is False


async def test_hold_lifts_when_the_pacific_day_rolls_over(archive):
    """Сутки у провайдера тихоокеанские — счёт по полуночи владельца
    ошибался бы на девять часов (ADR-0014)."""
    archive.llm_call_failed(
        status=429, purpose="enrich", at=quota.window_start(now) - timedelta(minutes=1)
    )
    assert await present.cosmetic_allowed(now) is True


async def test_cleanup_waits_out_the_quota_instead_of_dying(fake_model, archive):
    """Косметическое задание переставляет себя, а не исчерпывает попытки.

    Пять попыток с ростом задержки — это около получаса, а квота сбрасывается
    через часы: без переноса задание ушло бы в `failed` и ждало бы рук.
    """
    archive.llm_call_failed(status=429, purpose="enrich", at=now)
    await jobs.cleanup(
        entry_id=archive.voice_entry(number=26),
        chat_id=1,
        ack_message_id=5,
        model=fake_model,
        notifier=notifier,
    )
    assert fake_model.calls == []
    assert archive.queued(CLEANUP).execute_after == quota.resets_at(now)
```

Реализация:

```python
async def cosmetic_allowed(now: datetime) -> bool:
    """Можно ли тратить запрос на косметику.

    20 августа 2026 кончившаяся квота положила воркер вместе с расшифровкой
    и напоминаниями. Правило с тех пор явное: под нехваткой первыми отключаются
    косметические вызовы — чистка и лид сводки, — а захват, расшифровка,
    напоминания и разметка не трогаются никогда (ADR-0015, пункт 6).

    Признак — **наблюдённый отказ, а не настроенный лимит**: `429` от текстовой
    модели в текущем тихоокеанском окне. Лимит неизвестен и настройка пуста
    намеренно, так что механизм, ждущий числа, не сработал бы ни разу.

    Окно берётся из `core/quota`: сутки у провайдера тихоокеанские, и счёт
    по полуночи владельца ошибался бы на девять часов.
    """
```

Код ответа кладётся **отдельной колонкой** `llm_calls.status_code` (nullable),
а не вычитывается из текста ошибки: разбор строки разъедется с форматом
сообщения молча. Колонка добавляется миграцией этой же задачи, заполняется
в `llm.complete_over_entries` в ветке отказа.

- [x] **Шаг 9: `/find` тоже показывает вычищенное**

Найдено ревью Задачи 1, в план не попало по моему упущению. `core/retrieval/search.py`
кладёт в результат `Chunk.text`, а чанки строятся над **рабочим** текстом.
После чистки поиск остался бы единственным экраном, где владелец продолжает
видеть «э-э-э», — а сказано было «везде без исключений».

Выдача берёт `display_text()` записи, а не текст чанка.

**Цена названа вслух:** у записи длиннее 1500 символов чанков несколько,
и показанный кусок перестаёт быть тем куском, который совпал с запросом, —
показывается начало записи. Сегодня это задевает единицы записей (медиана 409
символов, максимум 1958), но с ростом архива будет задевать чаще. Настоящее
решение — вычищенные чанки, и оно приходит вместе с переключением рабочего
текста, а не раньше.

- [x] **Шаг 10: строка в `/status` и проверка модели при старте**

`/status` показывает «расшифровок без чистки: N» — отставание обязано быть
видно с телефона.

Модель чистки проверяется вызовом при старте воркера, но **валит старт она
не при каком коде ответа** — в отличие от расшифровки, эмбеддингов и разметки.
Правка внесена после ревью Задачи 2: первая редакция велела проверять её
наравне с тремя обязательными, а `check_models` на настроечном отказе делает
`SystemExit` — то есть неверное имя в `CLEANUP_MODEL` положило бы воркер вместе
с напоминаниями. Это ровно та поломка, против которой написан пункт 6 ADR-0015.

Счётчик отставания честен настолько, насколько может: приватные записи
из него исключены (чистить их нельзя по инварианту 5), а записи, на которых
модель вернула негодный ответ, останутся в нём навсегда — признака «пробовали
и не вышло» в схеме нет, и это названо долгом, а не спрятано.

- [x] **Шаг 11: зелёный, `just check`, дифф владельцу**

```
feat(present): clean up transcripts before showing them
```

---

## Задача 3 · Замер чистки

Без него модель чистки — дефолт, а не выбор, и так это и записано.

**Файлы:**
- Создать: `evals/cleanup/__init__.py`, `evals/cleanup/README.md`,
  `evals/cleanup/checks.py`, `evals/cleanup/run.py`, `evals/cleanup/dataset.jsonl`
- Изменить: `justfile`
- Тест: `tests/test_eval_cleanup_checks.py`

**Интерфейсы:**
- Потребляет: `present.cleanup.clean_one`
- Отдаёт наружу: `evals.cleanup.checks.compare(verbatim, clean) -> Report`
  с полями `new_words: list[str]`, `lost_numbers: list[str]`,
  `lost_names: list[str]`, `lost_negations: int`, `length_ratio: float`,
  `ok: bool`

- [x] **Шаг 1: тесты проверок — падают**

```python
def test_catches_invented_word():
    """Слово, которого не было в дословном варианте, — выдумка."""
    report = checks.compare("надо посмотреть фильм", "Надо обязательно посмотреть фильм.")
    assert "обязательно" in report.new_words
    assert not report.ok


def test_catches_changed_number():
    report = checks.compare("напомни в 19:40", "Напомни в 19:30.")
    assert report.lost_numbers == ["19:40"]
    assert not report.ok


def test_catches_dropped_negation():
    """Потерянное «не» переворачивает запись целиком."""
    report = checks.compare("решил не браться за это", "Решил браться за это.")
    assert report.lost_negations == 1
    assert not report.ok


def test_allows_removing_filler():
    report = checks.compare("э ну надо мм посмотреть фильм", "Надо посмотреть фильм.")
    assert report.ok
    assert report.new_words == []
```

- [x] **Шаг 2: красный → реализация проверок**

Сравнение по нормализованным словам (регистр, пунктуация не в счёт).
Числа, времена и даты — регуляркой. Имена собственные — слова с заглавной
в дословном варианте плюс латиница. Отрицания — счётчик «не»/«ни».
Порог длины: `LENGTH_FLOOR = 0.5` (падение больше половины — повод посмотреть
руками, а не отказ сам по себе).

- [x] **Шаг 3: прогон**

Шапка `run.py` называет режим прямо:

```python
"""Замер чистки. Режим: **по одной записи**, как её чистит воркер.

Пачками мерить нельзя: воркер чистит по одной, а на пачке модель видит соседние
записи и ведёт себя иначе. Ровно этой ошибкой два дня держался неверный
диагноз про темы (`status.md`, «Диагноз выше был неверен»).

Два уровня:
  1. код по всем голосовым архива — выдумки, числа, имена, отрицания, длина;
  2. набор владельца — сколько смысловых единиц дожило до чистой версии.

Ключ --model сравнивает модели на одном наборе.
"""
```

Печатает: доля записей без замечаний, список замечаний с номерами записей,
**медиану и p95 латентности** (из них выбирается `CLEANUP_WAIT_SEC`), стоимость.

- [x] **Шаг 4: формат набора владельца**

`evals/cleanup/README.md` объясняет, что размечать: для 10–15 записей выписать
смысловые единицы дословной версии своими словами. Прогон проверяет, что каждая
дожила. Правило то же, что у трёх существующих наборов: **размечает владелец,
а не модель**, иначе меряется согласие модели с собой.

`dataset.jsonl` кладётся пустым, с одной строкой-образцом и **номерами записей,
а не текстами** (ADR-0002).

- [x] **Шаг 5: `just eval-cleanup`, зелёный, дифф**

```
feat(evals): measure cleanup against the verbatim transcript
```

---

## Задача 4 · Названия дел и петель

**Файлы:**
- Создать: `src/brain/core/present/titles.py`,
  `src/brain/storage/migrations/versions/20260824_XXXX_titles.py`
- Изменить: `src/brain/storage/models/commitment.py`, `.../loop.py`
- Изменить: `src/brain/core/digest/render.py`, `src/brain/core/scheduler/jobs.py`
- Изменить: `src/brain/maintenance.py`, `justfile`
- Тест: `tests/test_present_titles.py`

**Интерфейсы:**
- Потребляет: `display_text()`, `llm.complete_over_entries`
- Отдаёт наружу:
  - `present.titles.name_open(model) -> int` — сколько названий проставлено
  - `present.titles.MAX_LENGTH = 50`, `present.titles.PURPOSE = "titles"`
  - `Commitment.title: Mapped[str | None]`, `Loop.title: Mapped[str | None]`

**Про идентификатор в пачке — названо вслух.** Дверь `complete_over_entries`
собирает конверт как `[id] текст`. Для названий `id` — это идентификатор **дела**,
а не записи: у одной записи бывает три дела, и идентификаторы записей в пачке
повторились бы. Признак приватности при этом едет **настоящий** — от исходной
записи, — поэтому дверь по-прежнему откажет, если приватное просочилось.

- [ ] **Шаг 1: тест — падает**

```python
async def test_summary_shows_title_not_span(archive):
    """Сводка показывает сформулированное название, а не вырезанный кусок."""
    commitment = archive.open_commitment(
        text="нужно запомнить посмотреть фильм Michael, когда все у меня сейчас уедут",
        title="Досмотреть фильм Michael",
    )
    line = render.morning(Morning(commitments=[commitment], open_commitments=1, loop=None))
    assert "Досмотреть фильм Michael" in line
    assert "нужно запомнить" not in line


async def test_falls_back_to_span_without_title(archive):
    """Названия ещё нет — показывается прежний отрезок, а не пустая строка."""
    commitment = archive.open_commitment(text="досмотреть фильм", title=None)
    assert "досмотреть фильм" in render.morning(
        Morning(commitments=[commitment], open_commitments=1, loop=None)
    )


def test_title_is_capped():
    assert len(titles.trim("а" * 200)) <= titles.MAX_LENGTH
```

- [ ] **Шаг 2: красный → миграция и колонки**

`title` nullable у обоих: пусто — деградация мягкая, показывается отрезок.

- [ ] **Шаг 3: вызов**

Промпт: короткое название в инфинитиве, до 50 символов, **словами из записи**,
без выдумывания и без вежливых оборотов. Температура 0.0, один вызов на пачку.
Вход — `display_text()`, то есть вычищенный: из чистого текста название выйдет
точнее, чем из сырого.

- [ ] **Шаг 4: проводка в разметку**

После `annotate_pending` заводится название для новых дел — тем же заданием
`ENRICH`, отдельным вызовом. Отказ названия не роняет разметку: дело без
названия работает.

- [ ] **Шаг 5: `just backfill-titles --dry-run` первым**

Как у остальных команд обслуживания: сначала печатает, скольким делам и петлям
нужно название и во сколько запросов это обойдётся, и только без `--dry-run`
делает. Считается **по данным** (`title IS NULL`), а не по журналу очереди.

- [ ] **Шаг 6: зелёный, прогон по живой базе**

```bash
just backfill-titles --dry-run   # ожидается: 70 дел, 1 петля, 3 запроса
```

- [ ] **Шаг 7: дифф владельцу**

```
feat(present): give commitments and loops written titles
```

---

## Задача 5 · Починка смены вопроса у петли

Дыра найдена при подготовке этого плана и подтверждена: `loops.is_stuck`
не вызывается **нигде** в `src/` — только в тестах. `digest/service.py:169`
зовёт `send_morning(...)` без `loop_stuck`, и значение остаётся `False`.
То есть смена вопроса после третьего «ещё думаю» (`vision.md` §5.2)
не работала ни разу с написания Фазы 4.

Тот же почерк, что у дыры №3 от 16 августа: каждый кусок проверен отдельно,
цепочка целиком — нет.

**Файлы:**
- Изменить: `src/brain/core/digest/service.py:166-171`
- Тест: `tests/test_digest_replay_findings.py`

- [ ] **Шаг 1: тест — падает на текущем коде**

```python
async def test_thrice_snoozed_loop_changes_the_question(notifier, archive):
    """После третьего «ещё думаю» вопрос меняется (`vision.md` §5.2).

    Проверяется на **проводке**, а не на `is_stuck`: чистая функция была верна
    и зелена всё это время, а до кнопок её ответ не доезжал.
    """
    loop_id = archive.loop(times_asked=3)
    await digest.tick(now=morning_moment, timezone="Europe/Rome", chat_id=1, notifier=notifier)
    assert notifier.morning_calls[-1].loop_stuck is True
```

- [ ] **Шаг 2: убедиться, что красный**

Ожидается: `assert False is True` — значение не доезжает.

- [ ] **Шаг 3: провести признак**

`_prepare` кладёт `loop_stuck` в нагрузку, `_send` передаёт его в `send_morning`.
Признак считается ядром, а не адаптером: правило про три откладывания — часть
замысла, а не оформления (так и записано в `ports.py`).

- [ ] **Шаг 4: зелёный, дифф**

```
fix(digest): actually switch the loop question after three snoozes
```

---

## Задача 6 · Проверка ответа модели

Строится до сводки, потому что сводка на неё опирается.

**Файлы:**
- Создать: `src/brain/core/present/guard.py`
- Тест: `tests/test_present_guard.py`

**Интерфейсы:**
- Отдаёт наружу:
  - `guard.check(written, *, expected_ids, source_digits, word_budget) -> str | None`
    — причина отказа или `None`, если ответ годится
  - `guard.reproach(text) -> str | None` — найденное слово упрёка
  - `guard.STOP_WORDS: frozenset[str]`

- [ ] **Шаг 1: тесты — падают**

```python
def test_rejects_unknown_id():
    """Модель написала про дело, которого ей не давали."""
    assert (
        guard.check(
            written_with_ids([7, 99]), expected_ids=[7], source_digits=set(), word_budget=80
        )
        is not None
    )


def test_rejects_lost_id():
    assert (
        guard.check(written_with_ids([7]), expected_ids=[7, 8], source_digits=set(), word_budget=80)
        is not None
    )


def test_rejects_invented_digit():
    """Дата, которой не было во входе, — выдумка, а не связка."""
    written = digest_text(lead="Досмотреть фильм до 15 сентября")
    assert guard.check(written, expected_ids=[7], source_digits={"7"}, word_budget=80) is not None


def test_rejects_over_budget():
    assert (
        guard.check(
            digest_text(lead="слово " * 200), expected_ids=[7], source_digits=set(), word_budget=80
        )
        is not None
    )


def test_catches_reproach_in_the_lead():
    """Упрёк ловится в тексте, а не в намерении разработчика.

    `revision_after_use.md`: правило §7.0 п.2 не нарушено в замысле — оно
    нарушено в реализации, и поймать это должен тест по тексту.
    """
    assert guard.reproach("У тебя накопилось три дела") is not None
    assert guard.reproach("Досмотреть фильм или снять его со списка?") is None


def test_friction_with_a_choice_is_allowed():
    """Трение назвать можно — но только вместе с выбором."""
    assert guard.reproach("Фильм переносится третий день — досмотреть или снять?") is None
```

- [ ] **Шаг 2: красный → реализация**

`STOP_WORDS`: «накопилось», «не сделал», «до сих пор не», «так и не», «опять не»,
«снова не», «забыл», «висит с». Правило выбора: строка со стоп-словом проходит,
только если содержит вопросительный знак и союз «или» — то есть предлагает
выбор, а не констатирует невыполнение.

- [ ] **Шаг 3: зелёный, дифф**

```
feat(present): validate model-written text before showing it
```

---

## Задача 7 · Сводка через модель

**Файлы:**
- Создать: `src/brain/core/present/digest.py`
- Изменить: `src/brain/core/digest/render.py`, `src/brain/core/digest/service.py`,
  `src/brain/core/digest/morning.py`, `src/brain/config.py`
- Тест: `tests/test_present_digest.py`

**Интерфейсы:**
- Потребляет: `guard.check`, `titles`, `cosmetic_allowed`
- Отдаёт наружу:
  - `present.digest.write(model, content) -> Written | None`
    с полями `lead: str`, `lines: dict[int, str]`, `loop_line: str | None`
  - `render.morning(content, written=None) -> str`
  - `Settings.digest_order: Literal["deeds_first", "loop_first"]`
  - `present.digest.WORD_BUDGET = 80`, `MAX_DEEDS = 3`

- [ ] **Шаг 1: тесты — падают**

```python
async def test_zero_data_means_zero_calls(fake_model):
    """Нечего показать — модель не зовётся вовсе.

    Не ради экономии: сводка ни о чём хуже отсутствия сводки.
    """
    written = await digest.write(fake_model, Morning(commitments=[], open_commitments=0, loop=None))
    assert written is None
    assert fake_model.calls == []


async def test_falls_back_to_dry_render_on_bad_answer(fake_model, archive):
    """Модель выдумала дело — приходит прежняя сухая сводка, а не ничего."""
    fake_model.answer = {"lead": "…", "items": [{"id": 999, "line": "выдумка"}]}
    content = archive.morning_with(deeds=2)
    text = render.morning(content, written=await digest.write(fake_model, content))
    assert "☀️ Утро." in text  # сухой рендер
    assert "выдумка" not in text


async def test_lead_names_one_thing_not_a_count(fake_model, archive):
    """Лид не пересказывает количество — владелец его и так видит."""
    content = archive.morning_with(deeds=3)
    written = await digest.write(fake_model, content)
    assert not re.search(r"\bтри дела\b|\b3 дела\b", written.lead)


def test_summary_fits_forty_seconds(archive):
    """Сорок секунд — проверяемый бюджет, а не пожелание."""
    text = render.morning(archive.morning_with(deeds=3), written=archive.written())
    assert len(text.split()) <= digest.WORD_BUDGET


def test_summary_asks_for_no_more_than_three_deeds(archive):
    content = archive.morning_with(deeds=10)
    assert len(content.commitments) <= digest.MAX_DEEDS


def test_order_is_a_setting_not_a_decision(archive, settings):
    """Что первым — дела или петля — выбирается наблюдением, а не автором."""
    settings.digest_order = "loop_first"
    text = render.morning(archive.morning_with(deeds=2, loop=True), written=archive.written())
    assert text.index("🔁") < text.index("1.")
```

- [ ] **Шаг 2: красный → вызов**

Вход модели — не голый список, а достаточный контекст, иначе лид будет пустым:
на каждое дело название, **возраст в днях**, сколько раз откладывали, тип.
Скупой вход даёт скупой лид.

Промпт требует: лид называет **одну конкретную вещь** — что сегодня важнее
прочего или что переносится не первый день; где отмечает трение, обязан давать
выбор («досмотреть или снять?»); пересказ количества запрещён прямо.

- [ ] **Шаг 3: сборка и падение**

`render.morning(content, written=None)` собирает сообщение из готовых строк.
`written is None` — прежний сухой рендер, слово в слово как сейчас. Сухой
рендер не удаляется никогда: он и есть защита от недетерминированности.

- [ ] **Шаг 4: `MAX_DEEDS = 3` вместо пяти**

`morning.COMMITMENTS_SHOWN` уезжает с 5 на 3. Остальные — по кнопке «все дела»
(задача 9).

**Названо вслух:** «не больше трёх действий» из `revision_after_use.md` я читаю
как «не больше трёх дел»; петля — четвёртая, необязательная, и оставлена
намеренно, потому что она ядро системы (`vision.md` §5.2). Если чтение неверно,
это одна строка.

- [ ] **Шаг 5: настройка порядка**

`DIGEST_ORDER=deeds_first|loop_first`, оба варианта под тестом. Выбирается
несколькими днями использования, а не автором.

- [ ] **Шаг 6: зелёный, `just check`, дифф**

```
feat(present): let the model write the morning summary
```

---

## Задача 8 · Убрать упрёк

**Файлы:**
- Изменить: `src/brain/core/digest/morning.py` (поле `unsorted` и его сбор),
  `src/brain/core/digest/render.py:65-68`
- Изменить: `src/brain/core/status.py` (число остаётся здесь)
- Тест: `tests/test_digest_no_reproach.py`

- [ ] **Шаг 1: тест — падает на текущем коде**

```python
def test_no_touch_text_contains_a_reproach(archive):
    """Стоп-детектор гоняется по всем тестам касаний, включая написанные моделью.

    До этой правки строка «Со вчера ждут разбора: 3. Покажу вечером.» проходила
    ревью и жила в проде — правило §7.0 п.2 было написано за три дня до неё.
    """
    for text in archive.every_touch_text():
        assert guard.reproach(text) is None


def test_unsorted_count_lives_in_status_only(archive):
    text = render.morning(archive.morning_with(deeds=1, unsorted=3), written=None)
    assert "ждут разбора" not in text
    assert "3" in await status.text()
```

- [ ] **Шаг 2: красный → удалить строку и поле**

`Morning.unsorted` уходит целиком: поле, которое больше некому показывать, —
это приглашение показать его снова.

- [ ] **Шаг 3: зелёный, дифф**

```
fix(digest): drop the retrospective reproach from the summary
```

---

## Задача 9 · Четыре состояния и кнопки

**Файлы:**
- Создать: `src/brain/storage/migrations/versions/20260824_XXXX_commitment_states.py`
- Изменить: `src/brain/storage/models/commitment.py`,
  `src/brain/core/digest/commitments.py`,
  `src/brain/adapters/telegram/digest_keyboards.py`,
  `src/brain/adapters/telegram/handlers.py:380-395`
- Тест: `tests/test_digest_commitment_states.py`

**Интерфейсы:**
- Отдаёт наружу:
  - `CommitmentState.IN_PROGRESS`, `CommitmentState.SNOOZED`
  - `Commitment.snoozed_until: Mapped[datetime | None]`
  - `commitments.set_state(id, state, *, now, until=None) -> bool`
  - `digest_keyboards.deed_choice_keyboard(commitment_id)`,
    `digest_keyboards.snooze_keyboard(commitment_id)`,
    `digest_keyboards.all_deeds_keyboard(ids, page)`

- [ ] **Шаг 1: тесты — падают**

```python
async def test_in_progress_stays_in_the_summary_at_its_age(archive):
    """«В работе» не поднимается наверх: это награда за начало, то есть счётчик."""
    old, started = archive.commitment(days=9), archive.commitment(days=2)
    await commitments.set_state(started.id, CommitmentState.IN_PROGRESS, now=now)
    shown = await commitments.open_ones()
    assert [item.id for item in shown] == [old.id, started.id]


async def test_snoozed_leaves_and_comes_back(archive):
    deed = archive.commitment(days=3)
    await commitments.set_state(
        deed.id, CommitmentState.SNOOZED, now=now, until=now + timedelta(days=7)
    )
    assert deed.id not in [item.id for item in await commitments.open_ones(now=now)]
    later = now + timedelta(days=8)
    assert deed.id in [item.id for item in await commitments.open_ones(now=later)]


def test_summary_keyboard_has_one_button_per_deed():
    """Двенадцать кнопок под сообщением — перегрузка (Д5)."""
    keyboard = digest_keyboards.morning_keyboard([1, 2, 3], loop_id=None, loop_stuck=False)
    assert sum(len(row) for row in keyboard.inline_keyboard) == 4  # три номера + «все дела»


def test_snooze_asks_for_how_long():
    rows = digest_keyboards.snooze_keyboard(7).inline_keyboard
    assert [button.text for button in rows[0]] == ["завтра", "через неделю", "через месяц"]
```

- [ ] **Шаг 2: красный → миграция**

```python
def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE необратим: значения остаются в типе навсегда,
    # и downgrade() вернёт схему не в точности прежней. То же свойство базы,
    # что записано ценой в ADR-0013.
    op.execute("ALTER TYPE commitment_state ADD VALUE IF NOT EXISTS 'in_progress'")
    op.execute("ALTER TYPE commitment_state ADD VALUE IF NOT EXISTS 'snoozed'")
    op.add_column(
        "commitments", sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True)
    )
```

- [ ] **Шаг 3: кнопки в три уровня**

```
[1] [2] [3] [все дела]
   ↓ 2
[✓ сделал] [▶ в работе] [💤 потом] [✕ неактуально] [‹ назад]
   ↓ потом
[завтра] [через неделю] [через месяц]
```

Состояние живёт в `callback_data`, как у всех кнопок проекта: перезапуск бота
их не ломает, своей таблицы ожидающих вопросов нет.

- [ ] **Шаг 4: «все дела» — что именно показывает**

Открытые дела, от старых к новым, **страницами по 20**: номер, название,
пометка ▶ у начатых. Отложенные не показываются до своего срока. Кнопки —
номера четырьмя рядами по пять плюс «дальше ›», когда есть ещё.

Страницы, а не весь список: сегодня открытых дел **70**, и семьдесят кнопок
под одним сообщением — это та же перегрузка, от которой чинится сводка.

- [ ] **Шаг 5: перерисовка после нажатия — кодом**

Ни одного вызова модели на нажатие: текст перерисовывается из сохранённой
нагрузки касания.

- [ ] **Шаг 6: зелёный, `just test-all`, дифф**

```
feat(digest): give commitments four states and compact buttons
```

---

## Задача 10 · Документы и приёмка

**Файлы:**
- Изменить: `docs/status.md`, `CLAUDE.md`, `docs/vision.md` §5.1,
  `docs/revision_after_use.md`, `docs/adr/0013-touch-rhythm.md` (блок
  «Обновление»), `docs/adr/0007-stt.md` (блок «Обновление»)

- [ ] **Шаг 1: `vision.md` §5.1**

Абзац «Собирается кодом, без вызова модели» заменяется на решение ADR-0015,
с сохранением того, **почему** прежнее решение было принято: срок проверки
назначался до реализации и сработал.

- [ ] **Шаг 2: блоки «Обновление» в ADR-0013 и ADR-0007**

Старые ADR не переписываются — к ним дописывается блок. Так же, как это сделано
в ADR-0011 дважды.

- [ ] **Шаг 3: `CLAUDE.md`**

Новый модуль в структуре, новые настройки, правило «лучше недочистить, чем
перечистить», правило приоритета под квотой.

- [ ] **Шаг 4: `status.md`**

- раздел про Волну 1 и что в ней закрыто;
- **новый долг:** модель чистки — дефолт, а не выбор замером, до прогона
  `just eval-cleanup` владельцем;
- **новый долг:** `CLEANUP_WAIT_SEC = 30` выбран, а не измерен, — как 13:00
  и `MIN_ENTRIES`;
- **новый долг:** `DIGEST_ORDER` не выбран, решается наблюдением;
- **точный лимит текстовой модели по-прежнему неизвестен** — но придерживание
  косметики его больше не ждёт: оно включается с первого `429`;
- **найденная дыра:** `is_stuck` не был проведён до кнопок (задача 5) —
  в раздел разборов, потому что это девятая дыра того же рода;
- **названо вслух:** приоритет под квотой не работает, пока
  `ENRICH_DAILY_QUOTA` пуст.

- [ ] **Шаг 5: приёмка волны — владельцем, не агентом**

```bash
just check && just test-all      # агент
just migrate                     # владелец
just backfill-titles --dry-run   # владелец: увидеть цену
just backfill-titles             # владелец: 3 запроса
just eval-cleanup                # владелец: живая модель
just eval-digest                 # владелец: повтор по прошедшим дням
```

Критерий волны, по которому судить: **сводка читается за 40 секунд, в ней нет
ни одного обрывка речи, и после неё хочется открыть бота завтра.** Первые два
проверяются тестами, третий — только неделей использования.

- [ ] **Шаг 6: дифф владельцу**

```
docs: record the presentation layer and what it costs
```

---

## Самопроверка плана

**Покрытие ревизии:** Д1 → задачи 1–3 · Д2 → задача 4 · Д3 → задачи 6–7 ·
Д4 → задача 8 · Д5 → задача 9. Мелочи из ответа владельца: `--dry-run` первым
(4.5), смена вопроса петли (задача 5), «все дела» определено (9.4), названия
из вычищенного текста (4.3).

**Согласованность имён:** `display_text`/`working_text` (1) используются в 2, 4,
7 · `guard.check`/`guard.reproach` (6) — в 7 и 8 · `cosmetic_allowed` (2) — в 7 ·
`CommitmentState.IN_PROGRESS`/`SNOOZED` (9) — только в 9.

**Что в плане названо ценой, а не спрятано:** `CLEANUP_WAIT_SEC` — число
из головы до замера латентности; счётчик отказов считает наши отказы, а не квоту
провайдера; сухой рендер живёт вечно вторым кодом; чистка старых записей
не делается; `DIGEST_ORDER` выбирается наблюдением, а не автором.

**Снято после ответа владельца:** «не больше трёх действий» — это три дела,
петля четвёртая и не в счёт, потому что она ядро системы; `MAX_DEEDS = 3`
подтверждён (семьдесят открытых дел — причина показывать мало, а не много);
приоритет под квотой перестал быть долгом и включается с первого `429`.
