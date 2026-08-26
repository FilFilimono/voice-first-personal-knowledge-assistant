"""Строка расхода перед командой обслуживания: расход **той модели**, что пойдёт.

До 26 августа 2026 команды печатали одно число на тир:

    Сегодня уже потрачено: 58. Суточный лимит неизвестен.

Пятьдесят восемь — это сумма по четырём моделям с разными лимитами, и на
единственный вопрос, ради которого строку читают («пойдёт ли прогон сейчас»),
она не отвечает. Лимит провайдер держит на модель — он сказал это дословно,
метрикой `GenerateRequestsPerDayPerProjectPerModel-FreeTier` в теле `429`.

Поэтому утверждения здесь идут **от команд**, а не от `_report_cost`: функция,
считающая по модели, ничего не стоит, пока `reindex` продолжает звать её тиром.
Ровно этой формы дыра описана в CLAUDE.md — «тест на функцию не заменяет теста
на путь».

День в `llm_calls` — настоящий, 26.08.2026: четыре модели, у одной двадцать
запросов кончились, у другой их за те же сутки двадцать два и ни одного отказа.
Живых вызовов к провайдеру тест не делает: модель названий подменена заглушкой,
а её имя взято настоящее — именно оно и должно доехать до строки расхода.
"""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import create_async_engine

from brain import maintenance
from brain.config import get_settings
from brain.core import llm
from brain.core.ports import Completion
from brain.core.retrieval.gemini import EMBEDDING
from brain.storage.db import session_scope
from brain.storage.models.commitment import Commitment
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.llm_call import LlmCall

pytestmark = pytest.mark.integration

EMBEDDINGS = "gemini-embedding-001"
ENRICH = "gemini-3.5-flash-lite"
TITLES = "gemini-3.6-flash"


@pytest.fixture(autouse=True)
def settings_from_env(migrated_database: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Модели задаются явно: `.env` владельца тестам не указ.

    Переменная окружения сильнее файла, поэтому прогон не зависит от того,
    что у владельца стоит в `ENRICH_MODEL` сегодня.
    """
    monkeypatch.setenv("EMBEDDING_MODEL", EMBEDDINGS)
    monkeypatch.setenv("EMBEDDING_DAILY_QUOTA", "1000")
    monkeypatch.setenv("ENRICH_MODEL", ENRICH)
    # Пусто — значит «лимит неизвестен»: у `3.5-flash-lite` он и правда не измерен.
    monkeypatch.setenv("ENRICH_DAILY_QUOTA", "")
    monkeypatch.setenv("TITLES_MODEL", TITLES)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Titler:
    """Модель названий, которая ничего не зовёт наружу, но честно себя называет.

    Имя настоящее: строка расхода обязана назвать ту модель, которой пойдёт
    прогон, — а прогон названий идёт именно этой.
    """

    def __init__(self, name: str = TITLES) -> None:
        self._name = name
        self.calls = 0

    @property
    def provider(self) -> str:
        return "stub"

    @property
    def model(self) -> str:
        return self._name

    @property
    def tier(self) -> str:
        return llm.CHEAP

    async def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any], temperature: float
    ) -> Completion:
        self.calls += 1
        return Completion(data={"items": []}, input_tokens=1, output_tokens=1, latency_ms=1)


async def _calls(model: str, *, tier: str, ok: int = 0, refused: int = 0) -> None:
    """Столько-то запросов к модели за сегодня, как их записал бы учёт."""
    now = datetime.now(UTC)
    async with session_scope() as session:
        for index in range(ok + refused):
            await session.execute(
                insert(LlmCall).values(
                    created_at=now,
                    provider="gemini",
                    model=model,
                    tier=tier,
                    purpose="cleanup",
                    input_tokens=0,
                    output_tokens=0,
                    entries_count=1,
                    latency_ms=1,
                    succeeded=index < ok,
                    status_code=None if index < ok else 429,
                )
            )


async def _today_as_it_really_was() -> None:
    """26 августа 2026 в `llm_calls`, числами из живой базы.

    Сумма по тиру `cheap` — те самые 58, из которых не следует ничего.
    """
    await _calls(TITLES, tier=llm.CHEAP, ok=19, refused=1)
    await _calls("gemini-3.5-flash", tier=llm.CHEAP, ok=17, refused=5)
    await _calls(ENRICH, tier=llm.CHEAP, ok=22)
    await _calls(EMBEDDINGS, tier=EMBEDDING, ok=1)


async def _entry(url: str, *, key: str) -> int:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            entry_id = await connection.scalar(
                insert(Entry)
                .values(
                    source=EntrySource.TELEGRAM,
                    kind=EntryKind.TEXT,
                    raw_text="надо досмотреть фильм и написать письмо",
                    is_private=False,
                    timezone="Europe/Rome",
                    dedup_key=key,
                    meta={},
                )
                .returning(Entry.id)
            )
        return int(entry_id or 0)
    finally:
        await engine.dispose()


async def _deeds(url: str, entry_id: int, count: int) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            for number in range(count):
                await connection.execute(
                    insert(Commitment).values(
                        source_entry_id=entry_id,
                        source_unit_id=None,
                        text="досмотреть фильм",
                        span_start=number * 10,
                        span_end=number * 10 + 16,
                        state="open",
                    )
                )
    finally:
        await engine.dispose()


async def test_reindex_reports_the_embedding_model(
    migrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Индексация идёт эмбеддингами — про них строка и должна быть."""
    await _today_as_it_really_was()
    await _entry(migrated_database, key="a")

    await maintenance.reindex(dry_run=True)

    shown = capsys.readouterr().out
    assert f"{EMBEDDINGS}: сегодня 1 из 1000" in shown
    assert "58" not in shown, "сумма по тиру вместо расхода модели"


async def test_reenrich_reports_the_enrich_model(
    migrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Разметка идёт `ENRICH_MODEL`, и её лимит честно неизвестен.

    Прежняя строка складывала её 22 запроса с расходом моделей, у которых
    лимит другой, и печатала 58 — число, ни к чему не относящееся.
    """
    await _today_as_it_really_was()
    await _entry(migrated_database, key="b")

    await maintenance.reenrich(dry_run=True)

    shown = capsys.readouterr().out
    assert f"{ENRICH}: сегодня 22" in shown
    assert "лимит неизвестен" in shown
    assert "58" not in shown, "сумма по тиру вместо расхода модели"


async def test_backfill_titles_reports_the_titles_model_and_its_refusals(
    migrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Названия идут `TITLES_MODEL` — той самой, у которой сегодня двадцать из двадцати.

    Отказ показывается вслух: прогон, который вот-вот упрётся в стену, лучше
    не начинать, а по сумме тира это было невидимо.
    """
    await _today_as_it_really_was()
    entry_id = await _entry(migrated_database, key="c")
    await _deeds(migrated_database, entry_id, 3)

    await maintenance.backfill_titles(model=Titler(), dry_run=True, recompute=False, limit=None)

    shown = capsys.readouterr().out
    assert f"{TITLES}: сегодня 19 из 20" in shown
    assert "отказов 1" in shown


async def test_preview_reports_the_titles_model_too(
    migrated_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--preview` зовёт ту же модель — и цену печатает про неё же."""
    await _today_as_it_really_was()
    entry_id = await _entry(migrated_database, key="d")
    await _deeds(migrated_database, entry_id, 2)

    await maintenance.backfill_titles(
        model=Titler(), dry_run=False, recompute=False, limit=None, preview=2
    )

    shown = capsys.readouterr().out
    assert f"{TITLES}: сегодня 19 из 20" in shown
