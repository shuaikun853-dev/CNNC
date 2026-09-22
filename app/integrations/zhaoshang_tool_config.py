# Canonical reusable capability; original script retains compatibility imports.
from __future__ import annotations
from pathlib import Path

REQUIRED_DB_VARIABLES = (
    "ZHAOSHANG_DB_HOST",
    "ZHAOSHANG_DB_PORT",
    "ZHAOSHANG_DB_USER",
    "ZHAOSHANG_DB_PASSWORD",
)

def load_zhaoshang_db_env(path):
    path = Path(path)
    values = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            cleaned = value.strip()
            if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'":
                cleaned = cleaned[1:-1]
            values[key.strip()] = cleaned
    missing = [key for key in REQUIRED_DB_VARIABLES if not values.get(key)]
    if missing:
        raise RuntimeError("招商局数据库配置缺少: " + ", ".join(missing))
    return {key: values[key] for key in REQUIRED_DB_VARIABLES}
