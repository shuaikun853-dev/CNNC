import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
SPEC = importlib.util.spec_from_file_location(
    "export_current_monitor_status",
    SCRIPT_DIR / "export_current_monitor_status.py",
)
assert SPEC and SPEC.loader
monitor_export = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(monitor_export)


@pytest.mark.parametrize("value", [1, 0.01, "12.50", " 3 "])
def test_valid_new_price_accepts_positive_numbers(value):
    assert monitor_export._is_valid_new_price(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        " ",
        "null",
        "None",
        "abc",
        "NaN",
        "Infinity",
        0,
        "0",
        -1,
        "-0.01",
        True,
    ],
)
def test_valid_new_price_rejects_missing_non_numeric_and_non_positive_values(
    value,
):
    assert monitor_export._is_valid_new_price(value) is False


@pytest.mark.parametrize(
    ("row", "pushable", "category"),
    [
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 1,
                "newPrice": "25.80",
            },
            "是",
            "可推送",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": "",
                "newPrice": 8,
            },
            "是",
            "可推送",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 2,
                "newPrice": 8,
            },
            "是",
            "可推送",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 5,
                "newPrice": 8,
            },
            "否",
            "价格检测校验失败",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 1,
                "syncPriceStatus": 1,
                "newPrice": "",
            },
            "否",
            "推送价格为空或无效",
        ),
        (
            {
                "isSync": 0,
                "isSyncStandard": 1,
                "syncPriceStatus": 1,
                "newPrice": 8,
            },
            "否",
            "采集未成功",
        ),
        (
            {
                "isSync": 1,
                "isSyncStandard": 2,
                "syncPriceStatus": 1,
                "newPrice": 8,
            },
            "否",
            "同款匹配异常/未成功",
        ),
    ],
)
def test_status_requires_successful_states_and_valid_push_price(
    row,
    pushable,
    category,
):
    result = monitor_export._status(row)
    assert result[3] == pushable
    assert result[4] == category
