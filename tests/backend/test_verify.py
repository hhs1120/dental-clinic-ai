"""숫자 후검증 (기능 정의 §4 ③·설계 2026-11-09 챗 "후처리 순서")."""

from backend.verify import allowed_numbers, numbers, numbers_ok


def test_numbers_strip_commas():
    assert numbers("본인부담 16,000원, 3일") == {16000, 3}


def test_row_count_allowed_only_when_asked():
    results = [{"count": 20, "appointments": [{"id": 1}] * 9}]
    assert numbers_ok("9건", allowed_numbers([], results, count_rows=True))
    assert not numbers_ok("9건", allowed_numbers([], results))


def test_nested_lists_counted():
    results = [{"by_day": {"mon": [1, 2, 3]}}]
    assert 3 in allowed_numbers([], results, count_rows=True)


def test_sum_and_new_numbers_blocked():
    assert not numbers_ok("합계 30,000원", allowed_numbers(["10,000원", "20,000원"]))


def test_numbers_from_results_and_sources():
    assert numbers_ok("16,000원, 12월 1일", allowed_numbers(["오늘 2026-12-01"], [{"fee": 16000}]))
