"""Приём события: дедупликация, приватность, запись и задание одной транзакцией.

Ядро не знает про Telegram: адаптер приносит сюда уже разобранное сообщение
и получает обратно текст, который надо показать владельцу. Из-за этого приём
проверяется быстрыми тестами без бота, без базы и без сети.
"""

from brain.core.ingest.service import (
    CaptureResult,
    IncomingText,
    IncomingVoice,
    capture_text,
    capture_voice,
)

__all__ = [
    "CaptureResult",
    "IncomingText",
    "IncomingVoice",
    "capture_text",
    "capture_voice",
]
