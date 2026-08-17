from datetime import date

import pytest

from dates import padding_scale, sample_date_windows


def test_full_window_when_all_images_requested():
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 20), num_images=-1, day_padding=5)
    assert windows == [(date(2018, 1, 1), date(2018, 1, 26))]


def test_three_samples_include_start_and_end():
    windows = sample_date_windows(date(2018, 1, 1), date(2018, 1, 31), num_images=3, day_padding=5)
    assert windows[0][0] == date(2018, 1, 1)
    assert windows[-1][0] == date(2018, 1, 31)
    assert len(windows) == 3


def test_rejects_short_sample_count():
    with pytest.raises(ValueError):
        sample_date_windows(date(2018, 1, 1), date(2018, 1, 10), num_images=2, day_padding=1)


def test_padding_scale_identity_and_double_area():
    assert padding_scale(1) == 1
    assert padding_scale(4) == 2
    assert padding_scale(2) == pytest.approx(2 ** 0.5)
