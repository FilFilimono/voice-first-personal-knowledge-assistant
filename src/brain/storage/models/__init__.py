"""Модели SQLAlchemy.

Каждая таблица — свой файл. Импортируются все: `Base.metadata` — то, с чем
autogenerate в Alembic сравнивает состояние базы, и модель, которую сюда
не внесли, для него не существует.

Таблиц очереди здесь нет: их схемой владеет pgqueuer, мы её не описываем
(ADR-0008).
"""

from brain.storage.models.annotation import Annotation, AnnotationState, AnnotationType
from brain.storage.models.base import Base
from brain.storage.models.chunk import DIMENSIONS, FTS_CONFIG, Chunk
from brain.storage.models.commitment import Commitment, CommitmentState
from brain.storage.models.entry import Entry, EntryKind, EntrySource
from brain.storage.models.llm_call import LlmCall
from brain.storage.models.loop import Loop, LoopStatus
from brain.storage.models.override import EntryOverride, OverrideAction
from brain.storage.models.private_mode import PrivateModeChange
from brain.storage.models.quiet import QuietKind, QuietPeriod
from brain.storage.models.reminder import Reminder, ReminderTrigger
from brain.storage.models.topic import EntryTopic, Topic, normalize
from brain.storage.models.touch import SilenceReason, Touch, TouchCategory, TouchSlot
from brain.storage.models.transcript import Transcript
from brain.storage.models.unit import EntryUnit

__all__ = [
    "DIMENSIONS",
    "FTS_CONFIG",
    "Annotation",
    "AnnotationState",
    "AnnotationType",
    "Base",
    "Chunk",
    "Commitment",
    "CommitmentState",
    "Entry",
    "EntryKind",
    "EntryOverride",
    "EntrySource",
    "EntryTopic",
    "EntryUnit",
    "LlmCall",
    "Loop",
    "LoopStatus",
    "OverrideAction",
    "PrivateModeChange",
    "QuietKind",
    "QuietPeriod",
    "Reminder",
    "ReminderTrigger",
    "SilenceReason",
    "Topic",
    "Touch",
    "TouchCategory",
    "TouchSlot",
    "Transcript",
    "normalize",
]
