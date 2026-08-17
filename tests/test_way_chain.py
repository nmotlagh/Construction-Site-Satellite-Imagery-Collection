from datetime import date

import pytest

from way_chain import WayChain, WayChainMap


@pytest.fixture(autouse=True)
def _reset_serial():
    WayChain.serial_no = 0
    yield
    WayChain.serial_no = 0


def _chain(way_id, serial=None):
    chain = WayChain(way_id, date(2018, 1, 1), date(2018, 2, 1), "building", "N/A", "N/A", None)
    if serial is not None:
        chain.serial_no = serial
    return chain


def test_find_key_does_not_substring_match():
    """Way 12 must not match a chain whose key/id is 123 (old ``str in key`` bug)."""
    wcm = WayChainMap()
    chain_123 = _chain(123)
    chain_12 = _chain(12)
    wcm.map[f"123-{chain_123.serial_no}"] = chain_123
    wcm.map[f"12-{chain_12.serial_no}"] = chain_12

    assert wcm.find_key(12) == f"12-{chain_12.serial_no}"
    assert wcm.find_key(123) == f"123-{chain_123.serial_no}"
    assert wcm.find_key("12") == f"12-{chain_12.serial_no}"


def test_find_key_missing_way_raises():
    wcm = WayChainMap()
    wcm.map["123-0"] = _chain(123)
    with pytest.raises(KeyError):
        wcm.find_key(12)


def test_increment_chain_post_appends_id():
    wcm = WayChainMap()
    chain = _chain(10)
    wcm.map[f"10-{chain.serial_no}"] = chain

    wcm.increment_chain(10, 20, is_prev=False)

    updated = wcm.get_way_chain(10)
    assert updated.ids == [10, 20]
    assert wcm.find_key(20) == f"10_20-{chain.serial_no}"


def test_increment_chain_prev_inserts_id_at_front():
    wcm = WayChainMap()
    chain = _chain(10)
    wcm.map[f"10-{chain.serial_no}"] = chain

    wcm.increment_chain(10, 5, is_prev=True)

    updated = wcm.get_way_chain(10)
    assert updated.ids == [5, 10]
    assert wcm.find_key(5) == f"5_10-{chain.serial_no}"
