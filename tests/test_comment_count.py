import math

from scripts.comment_count import normalize_comment_count


def test_normalize_comment_count_returns_numbers_or_none():
    assert normalize_comment_count(8) == 8
    assert normalize_comment_count(0) == 0
    assert normalize_comment_count("1,234+") == 1234
    assert normalize_comment_count("1.2万") == 12000
    assert normalize_comment_count("未识别") is None
    assert normalize_comment_count("旧的非数字值") is None
    assert normalize_comment_count("评论123") is None
    assert normalize_comment_count("") is None
    assert normalize_comment_count(None) is None
    assert normalize_comment_count(True) is None
    assert normalize_comment_count(-1) is None
    assert normalize_comment_count(math.inf) is None
