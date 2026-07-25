"""Утилиты"""

from .speakers import format_speaker_label
from .time_utils import format_duration_readable, format_time, format_timestamp_range

__all__ = [
    "format_speaker_label",
    "format_duration_readable",
    "format_time",
    "format_timestamp_range",
]
