"""Цены моделей: то, чем прогон отвечает на вопрос «сколько это стоило».

Проверяется не арифметика, а **знание**. 25 августа 2026 три модели чистки
сравнивались подряд, и каждый прогон заканчивался строкой «Стоил прогон:
$?»: `PRICES` про эти модели ничего не знал. Сравнить модели по деньгам
было нечем — то есть половина ответа, ради которого замер и гоняется,
не приходила вовсе.

Незнакомая модель по-прежнему даёт **пустую** стоимость, а не нулевую:
ноль означал бы «бесплатно».
"""

from decimal import Decimal

from brain.core.llm import pricing

# Ровно миллион токенов: тогда результат — это сама цена из прайса,
# и тест читается как строка со страницы провайдера.
MILLION = 1_000_000


def test_cleanup_candidates_are_priced() -> None:
    """Модели, которыми выбирают чистку, обязаны быть в прайсе.

    Иначе `just eval-cleanup --model ...` печатает `$?`, и выбор модели
    делается вслепую по одному качеству.
    """
    assert pricing.cost_usd("gemini-3.6-flash", input_tokens=MILLION, output_tokens=0) == Decimal(
        "0.75"
    )
    assert pricing.cost_usd("gemini-3.6-flash", input_tokens=0, output_tokens=MILLION) == Decimal(
        "3.75"
    )
    assert pricing.cost_usd("gemini-3.5-flash", input_tokens=MILLION, output_tokens=0) == Decimal(
        "1.50"
    )
    assert pricing.cost_usd("gemini-3.5-flash", input_tokens=0, output_tokens=MILLION) == Decimal(
        "9.00"
    )


def test_unknown_model_costs_nothing_known_rather_than_zero() -> None:
    """Пустая стоимость — это «не знаем», и путать её с «бесплатно» нельзя."""
    assert pricing.cost_usd("gemini-4-flash", input_tokens=MILLION, output_tokens=MILLION) is None
