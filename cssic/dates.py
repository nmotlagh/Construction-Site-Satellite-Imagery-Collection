"""Evenly sample construction-window dates for imagery requests."""

from __future__ import annotations

import math
from datetime import date, timedelta

DateWindow = tuple[date, date]


def sample_date_windows(
    start: date,
    end: date,
    num_images: int,
    day_padding: int,
) -> list[DateWindow]:
    """Return inclusive search windows for ``num_images`` samples.

    Samples are spread evenly across ``[start, end]`` and always include a
    window covering ``start`` and (when more than one sample is requested) a
    window covering ``end``. ``num_images == -1`` returns one window spanning
    the full construction period plus ``day_padding`` days after the end date,
    i.e. every available acquisition.

    Each window *reaches* exactly ``day_padding`` days past its sampled date --
    both ends are inclusive, so ``day_padding=0`` searches the sampled date
    alone and an image can never post-date its sample (or ``end``) by more than
    ``day_padding`` days.

    ``num_images`` may be any ``n >= 1`` or ``-1``; the v2 ``n >= 3`` rule is
    gone (1 and 2 are valid sample counts).
    """
    if end < start:
        raise ValueError(f"end date {end} is before start date {start}")
    if num_images != -1 and num_images < 1:
        raise ValueError(f"num_images must be >= 1 or -1, got {num_images}")
    if day_padding < 0:
        raise ValueError(f"day_padding must be >= 0, got {day_padding}")

    full_window = [(start, end + timedelta(days=day_padding))]
    if num_images == -1:
        return full_window
    if num_images == 1:
        return [(start, start + timedelta(days=day_padding))]
    if num_images == 2:
        return [
            (start, start + timedelta(days=day_padding)),
            (end, end + timedelta(days=day_padding)),
        ]

    days_in_range = (end - start).days - 1
    interior_count = num_images - 2
    if interior_count >= days_in_range:
        # More samples asked for than there are interior days: every day is a
        # sample. One window per day, not one spanning window -- a spanning
        # window yields a single pick, which would turn ``-n 5`` into one chip.
        return [
            (start + timedelta(days=offset), start + timedelta(days=offset + day_padding))
            for offset in range((end - start).days + 1)
        ]

    windows = [(start, start + timedelta(days=day_padding))]
    for i in range(1, interior_count + 1):
        days_add = timedelta(days=math.ceil(i * days_in_range / (interior_count + 1)))
        mid = start + days_add
        windows.append((mid, mid + timedelta(days=day_padding)))
    windows.append((end, end + timedelta(days=day_padding)))
    return windows


def padding_scale(padding: float) -> float:
    """Convert an area scale factor into the linear scale used on a bounding box.

    ``padding=1`` leaves the box unchanged. ``padding=2`` doubles the area
    (each side scaled by sqrt(2)) while staying center-invariant.
    """
    if padding <= 0:
        raise ValueError(f"padding must be > 0, got {padding}")
    return math.sqrt(padding)
