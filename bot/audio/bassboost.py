"""Уровни бас-буста и построение соответствующего ffmpeg-фильтра."""

from __future__ import annotations

from enum import StrEnum

_GAIN_DB: dict[str, float] = {
    "off": 0.0,
    "low": 6.0,
    "medium": 12.0,
    "high": 18.0,
    "extreme": 30.0,
}

_LABELS: dict[str, str] = {
    "off": "выключен",
    "low": "слабый",
    "medium": "средний",
    "high": "сильный",
    "extreme": "экстремальный",
}


class BassLevel(StrEnum):
    """Уровень бас-буста.

    На уровне EXTREME лимитер (alimiter) намеренно отключён: низкие частоты
    уходят в клиппинг при конвертации в s16le, бас звучит заметно громче.
    Это ожидаемое поведение, а не дефект.
    """

    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTREME = "extreme"

    @property
    def gain_db(self) -> float:
        """Прибавка усиления низких частот в децибелах."""
        return _GAIN_DB[self.value]

    @property
    def label(self) -> str:
        """Русское название уровня бас-буста."""
        return _LABELS[self.value]

    @classmethod
    def parse(cls, value: str) -> BassLevel:
        """Разбирает уровень бас-буста из строки регистронезависимо."""
        normalized = value.strip().lower()
        try:
            return cls(normalized)
        except ValueError as exc:
            raise ValueError(f"Неизвестный уровень бас-буста: {value!r}") from exc


def build_audio_filter(level: BassLevel) -> str | None:
    """Строит ffmpeg-фильтр бас-буста; для OFF возвращает None.

    Для LOW/MEDIUM/HIGH усиление идёт вместе с лимитером (alimiter), чтобы
    сигнал оставался чистым на умеренных уровнях. Для EXTREME лимитер
    отключён намеренно — клиппинг низких частот здесь является целью.
    """
    if level is BassLevel.OFF:
        return None
    gain = level.gain_db
    gain_str = f"{gain:g}"
    if level is BassLevel.EXTREME:
        return f"bass=g={gain_str}:f=110:w=0.6"
    return f"bass=g={gain_str}:f=110:w=0.6,alimiter=limit=0.95"
