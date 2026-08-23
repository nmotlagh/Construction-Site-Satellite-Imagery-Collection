from datetime import date, timedelta

import pytest

from cssic.dates import padding_scale, sample_date_windows


def test_full_window_when_all_images_requested():
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 20), num_images=-1, day_padding=5)
    assert windows == [(date(2018, 1, 1), date(2018, 1, 25))]


def test_three_samples_include_start_and_end():
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 31), num_images=3, day_padding=5)
    assert windows[0][0] == date(2018, 1, 1)
    assert windows[-1][0] == date(2018, 1, 31)
    assert len(windows) == 3


def test_single_sample_is_the_start_date():
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 31), num_images=1, day_padding=2)
    assert windows == [(date(2018, 1, 1), date(2018, 1, 3))]


def test_two_samples_are_the_endpoints():
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 31), num_images=2, day_padding=0)
    assert windows == [
        (date(2018, 1, 1), date(2018, 1, 1)),
        (date(2018, 1, 31), date(2018, 1, 31)),
    ]


def test_a_window_never_reaches_further_than_day_padding():
    """``--day-padding N`` means "N days", not "N + 1": no chip may post-date ``end``.

    The window ends are inclusive whole days downstream (STAC ``datetime=a/b``,
    Planet), so an extra ``+1`` here silently let an end-window acquisition land
    a week and a day after the construction chain finished.
    """
    start, end = date(2018, 1, 1), date(2018, 1, 31)
    for day_padding in (0, 1, 6):
        windows = sample_date_windows(start, end, num_images=4, day_padding=day_padding)
        for sampled, reach in windows:
            assert (reach - sampled).days == day_padding
        assert windows[-1][1] == end + timedelta(days=day_padding)


def test_interior_samples_are_placed_by_rounding_up():
    """Interior dates are ``ceil(i * days / (n - 1))`` from the start, not ``floor``."""
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 12), num_images=4, day_padding=2)
    assert [sampled for sampled, _reach in windows] == [
        date(2018, 1, 1),
        date(2018, 1, 5),
        date(2018, 1, 8),
        date(2018, 1, 12),
    ]


def test_more_samples_than_interior_days_samples_every_day():
    """Asking for as many interior samples as there are interior days takes them all.

    One window per day rather than one spanning window: a spanning window
    produces a single pick, and ``-n 5`` must not silently become one chip.
    """
    start, end = date(2018, 1, 1), date(2018, 1, 5)
    # (end - start).days - 1 == 3 interior days, and num_images=5 wants 3 of them.
    assert sample_date_windows(start, end, num_images=5, day_padding=1) == [
        (date(2018, 1, d), date(2018, 1, d + 1)) for d in range(1, 6)
    ]
    assert len(sample_date_windows(start, end, num_images=9, day_padding=1)) == 5
    assert len(sample_date_windows(start, end, num_images=4, day_padding=1)) == 4


def test_rejects_zero_samples():
    with pytest.raises(ValueError):
        sample_date_windows(date(2018, 1, 1), date(2018, 1, 10), num_images=0, day_padding=1)


def test_rejects_reversed_window():
    with pytest.raises(ValueError):
        sample_date_windows(date(2018, 1, 10), date(2018, 1, 1), num_images=3, day_padding=1)


def test_padding_scale_identity_and_double_area():
    assert padding_scale(1) == 1
    assert padding_scale(4) == 2
    assert padding_scale(2) == pytest.approx(2**0.5)


def test_padding_scale_rejects_non_positive():
    with pytest.raises(ValueError):
        padding_scale(0)
