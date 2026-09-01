"""Две стороны замера: облачный Gemini и локальный Parakeet.

**Зачем две.** Долг, ради которого замер делается, звучит так: «мерить обе
стороны на одном корпусе, иначе это снова сравнение измеренного
с неизмеренным». Эта ошибка проекту уже стоила двух дней на моделях чистки.
Поэтому движок здесь — ключ командной строки, а не настройка в `.env`: один
корпус, два прогона, одни и те же эталоны.

**Локальная сторона не потребовала ни одной новой зависимости, и это выяснилось
проверкой, а не рассуждением.** Владелец уже диктует через Handy — приложение
с локальным Parakeet TDT 0.6B v3 (GGUF, Metal). У него есть режим командной
строки `--transcribe-file`, который берёт WAV, ничего не скачивает и микрофон
не трогает. Звук из Telegram приходит в OGG/Opus, и разворачивает его
`afconvert` — он лежит в macOS из коробки. То есть локальный движок для замера
здесь — это **два процесса, а не пакет**: `pyproject.toml` не меняется,
и ADR под замер не нужен.

**Чего это не решает.** Прод так работать не будет: воркер не может поднимать
GUI-приложение на каждое голосовое. Если замер покажет, что локальный движок
слышит имена лучше, — вот тогда понадобится настоящая реализация
`core/stt/local.py`, и **вот там** появляется выбор зависимости. Он вынесен
в черновик ADR-0016 и принимается владельцем, а не замером.

Проверено 27.08.2026 на настоящей записи архива: `.oga` из `var/audio`
разворачивается `afconvert` и расшифровывается Handy за 0.87 с при 24 с звука
(27× реального времени, Metal, M3).
"""

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from brain.config import Settings
from brain.core import stt
from brain.core.ports import Transcriber, Transcription
from brain.core.stt.errors import TranscriptionError

GEMINI = "gemini"
HANDY = "handy"

# Порядок важен только для сообщения об ошибке: первым идёт тот, на котором
# собран архив, — с ним и сравнивают.
ENGINES = (GEMINI, HANDY)

# Приложение владельца. Путь захардкожен намеренно: это не настройка проекта,
# а инструмент на машине, где гоняется замер. Ключ `--handy` в прогоне даёт
# заменить его, не трогая код.
HANDY_BINARY = Path("/Applications/Handy.app/Contents/MacOS/handy")

# Разворачивает OGG/Opus в WAV. Часть macOS, ставить нечего — ровно поэтому
# он и выбран вместо ffmpeg, которого на машине нет.
AFCONVERT = "afconvert"

# 16 кГц моно — то, чего ждёт Parakeet. Пересчитывать за движок надёжнее,
# чем полагаться на то, что он это сделает сам.
WAV_FORMAT = ("-f", "WAVE", "-d", "LEI16@16000", "-c", "1")

# Расширение по типу содержимого: `afconvert` смотрит и на него тоже.
SUFFIXES = {"audio/ogg": ".oga", "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/mpeg": ".mp3"}
DEFAULT_SUFFIX = ".audio"

NO_HANDY = (
    "локального движка нет: не найден {binary}\n"
    "Замер сравнивает Gemini с тем же Handy, которым владелец диктует, —\n"
    "приложение должно быть установлено, а модель уже скачана (замер не качает).\n"
    "Другой путь к приложению: --handy /путь/к/handy"
)

NO_AFCONVERT = (
    "нет afconvert — не во что развернуть OGG/Opus.\n"
    "Он входит в macOS; на другой системе локальная сторона замера не работает."
)

NO_KEY = (
    "GOOGLE_API_KEY не задан, а облачная сторона замера без него не ходит.\n"
    "Локальная сторона ключа не требует: --engine handy."
)


@dataclass(frozen=True, slots=True)
class Spoken:
    """Ответ локального движка: текст и то, из чего сложилось его время."""

    text: str
    model: str
    load_ms: int
    transcribe_ms: int


class HandyTranscriber:
    """Локальный Parakeet через командный режим Handy.

    Реализует тот же протокол `Transcriber`, что и облачный провайдер, — иначе
    прогон пришлось бы писать в двух вариантах, и две стороны замера перестали
    бы быть одним замером.
    """

    def __init__(self, *, binary: Path = HANDY_BINARY, model: str | None = None) -> None:
        self._binary = binary
        self._requested = model
        # Настоящее имя модели известно только из ответа: без `--model` Handy
        # берёт ту, что выбрана в его настройках. Отчёт берёт имя из ответов
        # (см. `run.report`), поэтому здесь оно нужно лишь для сообщений.
        self._model = model or "по настройке Handy"

    @property
    def provider(self) -> str:
        return HANDY

    @property
    def model(self) -> str:
        return self._model

    async def check_available(self) -> None:
        """Есть ли чем расшифровывать. Настоящего вызова нет — он стоит секунд.

        В отличие от облачной модели, локальная не снимается с обслуживания
        и не отвечает 404: проверить надо только наличие двух программ.
        """
        if not self._binary.exists():
            raise SystemExit(NO_HANDY.format(binary=self._binary))
        if shutil.which(AFCONVERT) is None:
            raise SystemExit(NO_AFCONVERT)

    async def transcribe(self, audio: bytes, mime_type: str) -> Transcription:
        """Расшифровать один файл. Ни сети, ни ключа, ни загрузки модели."""
        await self.check_available()

        with TemporaryDirectory() as directory:
            source = Path(directory) / f"audio{SUFFIXES.get(mime_type, DEFAULT_SUFFIX)}"
            source.write_bytes(audio)
            wav = await self._to_wav(source)
            spoken = await self._run(wav)

        self._model = spoken.model
        return Transcription(
            text=spoken.text,
            provider=HANDY,
            model=spoken.model,
            # Время движка, а не время прогона: запуск процесса и разворот
            # звука — свойство замера, а не движка. Общее время прогон меряет
            # отдельно и печатает рядом, чтобы разница была видна.
            latency_ms=spoken.load_ms + spoken.transcribe_ms,
        )

    async def _to_wav(self, source: Path) -> Path:
        if source.suffix == ".wav":
            return source

        target = source.with_suffix(".wav")
        await _execute(
            [AFCONVERT, *WAV_FORMAT, str(source), str(target)],
            failure="afconvert не развернул звук",
        )
        return target

    async def _run(self, wav: Path) -> Spoken:
        command = [str(self._binary), "--transcribe-file", str(wav), "--json"]
        if self._requested:
            command += ["--model", self._requested]

        answer = await _execute(command, failure="Handy не расшифровал файл")
        return _parse(answer)


def _parse(answer: str) -> Spoken:
    """Разобрать JSON Handy. Формат сверен с настоящим ответом 27.08.2026."""
    try:
        payload = json.loads(answer)
        text = str(payload["text"]).strip()
        # `transcribe_ms` — список: у Handy есть `--repeat`, и он кладёт время
        # каждого прогона. Мы зовём один раз, но брать `[0]` вслепую нельзя.
        attempts = [int(value) for value in payload.get("transcribe_ms", [])]
        spoken = Spoken(
            text=text,
            model=str(payload.get("model", "")),
            load_ms=int(payload.get("load_ms", 0)),
            transcribe_ms=min(attempts) if attempts else int(payload.get("best_ms", 0)),
        )
    except (ValueError, KeyError, TypeError) as error:
        raise TranscriptionError(f"ответ Handy не разобран: {error}") from None

    if not spoken.text:
        # То же правило, что у облачного провайдера (ADR-0009): пустой ответ —
        # ошибка, а не «в записи была тишина». Молча зачтённая пустая строка
        # дала бы WER 100% и выглядела бы как плохой движок.
        raise TranscriptionError("Handy вернул пустой текст")
    return spoken


async def _execute(command: list[str], *, failure: str) -> str:
    """Запустить внешнюю программу и вернуть её `stdout`.

    `stderr` не смешивается со `stdout`: Handy пишет туда подробный лог
    инициализации Metal, и склеенный вывод перестал бы разбираться как JSON.
    """
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()
    if process.returncode != 0:
        raise TranscriptionError(f"{failure}: {err.decode(errors='replace').strip()[-300:]}")
    return out.decode()


def build(
    engine: str,
    settings: Settings,
    *,
    model: str | None = None,
    binary: Path | None = None,
) -> Transcriber:
    """Движок по имени. Отказ — до чтения корпуса, а не на первой записи."""
    if engine == GEMINI:
        if not settings.google_api_key.strip():
            raise SystemExit(NO_KEY)
        # Через ту же дверь, что и воркер: замер обязан мерить рабочий код,
        # а не свою копию запроса.
        return stt.build_transcriber(
            settings.model_copy(update={"stt_model": model}) if model else settings
        )

    if engine == HANDY:
        return HandyTranscriber(binary=binary or HANDY_BINARY, model=model)

    raise SystemExit(f"неизвестный движок «{engine}». Есть: {', '.join(ENGINES)}")
