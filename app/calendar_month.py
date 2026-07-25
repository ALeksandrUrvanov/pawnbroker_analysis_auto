"""Календарный месяц от опорной даты (свод БД, CLI)."""
from __future__ import annotations

from datetime import date


def previous_month_y_m(ref: date) -> tuple[int, int]:
    if ref.month == 1:
        return ref.year - 1, 12
    return ref.year, ref.month - 1


def previous_month_yyyy_mm(ref: date) -> str:
    y, m = previous_month_y_m(ref)
    return f"{y:04d}-{m:02d}"


def current_month_yyyy_mm(ref: date) -> str:
    return f"{ref.year:04d}-{ref.month:02d}"
