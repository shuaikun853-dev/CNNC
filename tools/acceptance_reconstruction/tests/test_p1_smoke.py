# -*- coding: utf-8 -*-
"""真实文件 smoke（默认 deselect, 用 -m smoke 显式运行; 慢）。

    python.exe -X utf8 -m pytest tests/test_p1_smoke.py -m smoke -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CFG_PATH = r"E:\.A中核运维\_acceptance_reconstruction_20260911\config.local.json"

pytestmark = pytest.mark.smoke

EXPECTED_OUTPUTS = [
    "final_main.parquet", "final_problems.parquet", "p25.parquet",
    "s3flags.parquet", "drops.parquet", "dify.parquet",
    "comments_final.parquet", "reccsv_agg.parquet",
]


def test_smoke_full_extract():
    import p1_extract

    rc = p1_extract.main(["--config", CFG_PATH, "--only", "all"])
    assert rc == 0
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for fn in EXPECTED_OUTPUTS:
        p = os.path.join(cfg["temp_dir"], fn)
        assert os.path.exists(p), f"缺少产物 {p}"
    stats_path = os.path.join(cfg["workdir"], "inventory", "p1_stats.json")
    assert os.path.exists(stats_path)
    with open(stats_path, "r", encoding="utf-8") as f:
        stats = json.load(f)
    assert stats["final"]["cross_check"] == "PASS"
    assert stats["final"]["rows"] == 418291
    assert stats["comments"]["rows"] == 418291
