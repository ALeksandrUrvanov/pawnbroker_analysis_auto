"""Форматирование времени и длительностей."""


def format_duration_readable(seconds: float) -> str:
    """Длительность для логов: «5 мин 30 сек» или «45 сек»."""
    total = int(round(seconds or 0))
    total = max(0, total)
    minutes = total // 60
    secs = total % 60
    if minutes > 0:
        return f"{minutes} мин {secs} сек"
    return f"{secs} сек"


def format_time(seconds: float) -> str:
    """Секунды → строка «ЧЧ:ММ:СС»."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_timestamp_range(start: float, end: float) -> str:
    """Диапазон для транскрипции: «[ЧЧ:ММ:СС - ЧЧ:ММ:СС]»."""
    return f"[{format_time(start)} - {format_time(end)}]"
