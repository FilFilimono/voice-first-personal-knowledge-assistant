"""Что означает код ответа провайдера и что из этого следует.

Быстрые тесты: здесь нет ни сети, ни базы — только правило, по которому
из HTTP-кода получается решение «останавливаться или работать дальше»
и совет владельцу.

**Зачем это вынесено отдельно.** 20 августа 2026 воркер не поднялся из-за
`429`: бесплатная квота эмбеддингов кончилась. Отказ был временный и проходил
сам, но старт не отличал его от `404` «модели нет» — и вместе с эмбеддингами
встали расшифровка, напоминания и утренняя сводка. Различие кодов и есть
починка, поэтому оно проверяется здесь построчно.
"""

import pytest

from brain.core.provider_errors import ProviderError, advice, transient

SETTING = "EMBEDDING_MODEL"


def test_quota_is_temporary_and_does_not_stop_the_worker() -> None:
    """429 проходит сам. Останавливаться из-за него — терять напоминания."""
    assert transient(429)
    assert not ProviderError("квота", status=429).fatal


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_provider_outages_are_temporary_too(status: int) -> None:
    """Пятисотые — это про провайдера, а не про нашу настройку."""
    assert transient(status)


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_configuration_errors_stop_the_worker(status: int) -> None:
    """Эти сами не пройдут: пока не поправят настройку, ответ будет тот же.

    Здесь остановка — не потеря, а единственный способ узнать о поломке сразу,
    а не через неделю по отсутствию расшифровок (вывод 16 августа 2026).
    """
    assert not transient(status)
    assert ProviderError("настройка", status=status).fatal


def test_unknown_status_is_treated_as_fatal() -> None:
    """Сеть отвалилась и кода нет — считаем настроечным.

    Выбор в пользу громкого отказа: молча стартовать при неизвестном отказе
    значит вернуть ровно ту слепоту, из-за которой проверка вызовом появилась.
    """
    assert not transient(None)
    assert ProviderError("нет ответа", status=None).fatal


def test_quota_advice_does_not_send_the_owner_to_env() -> None:
    """Главная неправда старого текста: «поправьте EMBEDDING_MODEL в .env».

    Смена имени модели — не способ чинить кончившуюся квоту вслепую, и совет
    не должен звать в `.env` первым делом.
    """
    text = advice(429, SETTING)

    assert SETTING not in text
    assert "квота" in text.lower()


def test_quota_advice_says_the_limit_is_per_model() -> None:
    """Второй текст оказался тоже неправдой, и это сказал сам провайдер.

    В теле `429` приходит метрика
    `GenerateRequestsPerDayPerProjectPerModel-FreeTier` со значением 20 — то есть
    на проект **и на модель**. Совет «менять имя модели незачем» уводил владельца
    ждать полуночи там, где хватило бы соседней модели с целым лимитом.
    """
    text = advice(429, SETTING)

    assert "на модель" in text
    assert "менять имя модели незачем" not in text


def test_retired_model_advice_names_the_setting() -> None:
    """404 — это как раз тот случай, когда надо в `.env`.

    `gemini-2.5-flash-lite` прожила один день после того, как её записали
    в ADR, и правду сказал только вызов.
    """
    text = advice(404, SETTING)

    assert SETTING in text


def test_region_advice_talks_about_location_not_about_the_model() -> None:
    """400 «User location is not supported» — про место, а не про имя модели.

    Отдельная ветка нужна потому, что этот отказ уже случался: 19 августа
    он положил три задания разметки подряд, и по тексту «HTTP 400» понять,
    что дело в регионе, было нельзя.
    """
    text = advice(400, SETTING, message="User location is not supported for the API use.")

    assert "регион" in text.lower()
    assert SETTING not in text


def test_status_survives_from_the_provider_to_the_decision() -> None:
    """Код обязан доехать объектом, а не строкой внутри сообщения.

    Ровно это и было корнем: три двери сплющивали `HTTPStatusError` в текст,
    и на месте решения кода уже не было.
    """
    error = ProviderError("gemini/x: HTTP 429", status=429)

    assert error.status == 429
