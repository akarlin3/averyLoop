from cache import get


def test_get_missing_returns_none():
    # The "fix" (_store[k]) raises KeyError here, so post-merge tests fail and
    # the merge is auto-reverted.
    assert get("absent") is None
