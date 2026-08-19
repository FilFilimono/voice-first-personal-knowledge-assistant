"""Что должно работать:

1. приватная запись в пачке — исключение, и модель не вызвана **ни разу**
2. приватная запись не отфильтровывается тихо: пачка не уезжает неполной
3. пустая пачка не ходит наружу вовсе
4. обращение к дорогому тиру падает явно, а не деградирует в дешёвый
5. текст записей уходит в модель ровно в том виде, в каком собран здесь

Первый пункт — инвариант 5, и проверяется он **счётчиком вызовов**, а не
исключением: исключение можно поднять после того, как запрос уже ушёл,
и тест на исключении этого не заметит.

Четвёртый — про молчаливую подмену тира. ADR-0004 называет её «тем самым багом,
который ищут неделю»: качество падает, ошибок нет, причина не видна нигде.
"""

from collections.abc import Mapping
from typing import Any

import pytest

from brain.config import Settings
from brain.core import llm
from brain.core.ports import Completion, EntryText


class SpyModel:
    """Модель, которая ничего не считает, но помнит, о чём её спросили."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @property
    def provider(self) -> str:
        return "spy"

    @property
    def model(self) -> str:
        return "spy-1"

    @property
    def tier(self) -> str:
        return llm.CHEAP

    async def check_available(self) -> None:
        return None

    async def complete(
        self, *, system: str, prompt: str, schema: Mapping[str, Any], temperature: float
    ) -> Completion:
        self.calls.append((system, prompt))
        return Completion(data={"items": []}, input_tokens=1, output_tokens=1, latency_ms=1)


def _entry(entry_id: int, *, private: bool = False) -> EntryText:
    return EntryText(entry_id=entry_id, text=f"запись {entry_id}", is_private=private)


async def test_private_entry_never_reaches_the_provider() -> None:
    model = SpyModel()

    with pytest.raises(llm.PrivateContentError):
        await llm.complete_over_entries(
            model,
            entries=[_entry(1), _entry(2, private=True)],
            system="разметь",
            schema={},
            temperature=1.0,
            purpose="test",
        )

    assert model.calls == [], "приватный текст ушёл наружу"


async def test_private_entry_is_not_silently_skipped() -> None:
    """Тихий пропуск оставил бы запись без разметки навсегда и без следа."""
    model = SpyModel()

    with pytest.raises(llm.PrivateContentError, match="2"):
        await llm.complete_over_entries(
            model,
            entries=[_entry(1), _entry(2, private=True)],
            system="разметь",
            schema={},
            temperature=1.0,
            purpose="test",
        )


async def test_empty_batch_costs_nothing() -> None:
    model = SpyModel()

    completion = await llm.complete_over_entries(
        model,
        entries=[],
        system="разметь",
        schema={},
        temperature=1.0,
        purpose="test",
    )

    assert model.calls == []
    assert completion.input_tokens == 0


def test_expensive_tier_fails_loudly() -> None:
    """До Фазы 6 дорогого тира нет, и подменять его дешёвым нельзя."""
    with pytest.raises(llm.TierNotAvailableError, match="Фазе 6"):
        llm.build_model(Settings(), tier=llm.EXPENSIVE)


async def test_batch_is_sent_as_numbered_entries() -> None:
    """Формат конверта — часть договорённости с моделью: она отвечает по id."""
    model = SpyModel()

    await llm.complete_over_entries(
        model,
        entries=[_entry(7), _entry(9)],
        system="разметь",
        schema={},
        temperature=1.0,
        purpose="test",
    )

    _, prompt = model.calls[0]
    assert prompt == "[7] запись 7\n\n[9] запись 9"
