"""Дверь наружу для текста записей.

Здесь стоит проверка инварианта 5, и стоит она именно здесь, а не в вызывающем
коде: вызывающий не обязан помнить о приватности и не должен уметь её обойти.
Так же устроен `core/stt`, и по той же причине.

Разница с расшифровкой одна, и она важна. Приватное голосовое **не принимается
вовсе** — расшифровать его нечем, кроме внешнего сервиса. Приватная текстовая
запись принимается и хранится: у неё есть текст, значит есть чанки и `tsv`,
значит она находится полнотекстом и триграммами. Наружу не уходит только
эмбеддинг. Приватная запись ищется хуже — это прямое следствие ADR-0002,
принятое там же и названное вслух.
"""

from brain.config import Settings
from brain.core.ports import Embedder
from brain.core.retrieval.errors import (
    EmbeddingError,
    PrivateContentError,
    ProviderUnavailableError,
)
from brain.core.retrieval.gemini import GeminiEmbedder


def build_embedder(settings: Settings) -> Embedder:
    """Провайдер по настройке. Пока он один — второй придёт со сверкой моделей."""
    match settings.embedding_provider:
        case "gemini":
            return GeminiEmbedder(
                api_key=settings.google_api_key,
                model=settings.embedding_model,
            )


async def embed_entry_chunks(
    embedder: Embedder,
    *,
    texts: list[str],
    is_private: bool,
) -> list[list[float]]:
    """Посчитать вектора кусков записи. Единственная дверь наружу для текста.

    У приватной записи векторов не бывает: поднимается исключение, а не
    возвращается пустой список. Пустой список — законный ответ на пустой ввод,
    и спутать «нечего считать» с «нельзя считать» здесь нельзя ни при каких
    обстоятельствах.
    """
    if is_private:
        raise PrivateContentError(
            "приватная запись не уходит в модель эмбеддингов (инвариант 5): "
            "она ищется полнотекстом и триграммами"
        )

    return await embedder.embed_documents(texts)


async def embed_topic_names(embedder: Embedder, names: list[str]) -> list[list[float]]:
    """Посчитать вектора названий тем.

    Отдельная дверь, а не вызов провайдера напрямую, — чтобы весь текст,
    уходящий наружу, уходил через этот файл и был здесь виден списком.

    Проверки приватности тут нет, и это не пропуск. Имя темы — не текст записи,
    а обобщение вроде «подготовка к CV»; и главное, темы заводятся только
    на **размеченных** записях, а приватные до разметки не доходят вовсе
    (см. `core/enrich/repository.entries_needing_annotation`). Если этот порядок
    когда-нибудь изменится, проверку надо будет добавить сюда, а не у вызывающего.
    """
    if not names:
        return []
    return await embedder.embed_documents(names)


__all__ = [
    "Embedder",
    "EmbeddingError",
    "PrivateContentError",
    "ProviderUnavailableError",
    "build_embedder",
    "embed_entry_chunks",
    "embed_topic_names",
]
