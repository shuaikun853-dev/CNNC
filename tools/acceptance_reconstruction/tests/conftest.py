# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# duckdb/pyarrow 实际所在目录（与 config.local.json 的 libs_dir 一致）。
# 单测在 connect_duckdb 之前就要写 mini parquet, 必须提前可导入 pyarrow。
_LIBS = r"E:\.A中核运维\_cnnc_comment_index\_libs"
if os.path.isdir(_LIBS) and _LIBS not in sys.path:
    sys.path.insert(0, _LIBS)
