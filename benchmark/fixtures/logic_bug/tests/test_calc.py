from calc import range_sum


def test_range_sum_includes_n():
    # 0 + 1 + 2 + 3 = 6 ; fails while range_sum uses range(n) -> 0+1+2 = 3
    assert range_sum(3) == 6
