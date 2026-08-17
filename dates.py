"""Evenly sample construction-window dates for imagery requests."""

from __future__ import annotations

import math
from datetime import date, timedelta


def sample_date_windows(
    start: date,
    end: date,
    num_images: int,
    day_padding: int,
) -> list[tuple[date, date]]:
    """Return inclusive search windows for ``num_images`` samples.

    Always includes a window covering ``start`` and a window covering ``end``.
    ``num_images == -1`` returns one window spanning the full construction
    period plus ``day_padding`` days after the end date.
    """
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    if num_images != -1 and num_images < 3:
        raise ValueError(f"num_images must be >= 3 or -1, got {num_images}")
    if day_padding < 0:
        raise ValueError(f"day_padding must be >= 0, got {day_padding}")

    full_window = [(start, end + timedelta(days=1 + day_padding))]
    if num_images == -1:
        return full_window

    days_in_range = (end - start).days - 1
    interior_count = num_images - 2
    if interior_count >= days_in_range:
        return full_window

    windows = [(start, start + timedelta(days=1 + day_padding))]
    for i in range(1, interior_count + 1):
        days_add = timedelta(days=math.ceil(i * days_in_range / (interior_count + 1)))
        mid = start + days_add
        windows.append((mid, mid + timedelta(days=1 + day_padding)))
    windows.append((end, end + timedelta(days=1 + day_padding)))
    return windows


def padding_scale(padding: float) -> float:
    """Convert an area scale factor into the linear scale used on a bounding box.

    ``padding=1`` leaves the box unchanged. ``padding=2`` doubles the area
    (each side scaled by sqrt(2)) while staying center-invariant.
    """
    if padding <= 0:
        raise ValueError(f"padding must be > 0, got {padding}")
    return math.sqrt(padding)
