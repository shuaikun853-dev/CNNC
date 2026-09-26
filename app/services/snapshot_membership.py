"""Resolve physical membership without changing authoritative observation IDs."""

import sqlite3


def membership_source_id(connection: sqlite3.Connection, snapshot_id: int) -> int:
    # SELECT * keeps historical read-only databases usable without migration.
    row = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is not None and "membership_source_id" in row.keys() and row["membership_source_id"] is not None:
        return int(row["membership_source_id"])
    return int(snapshot_id)


def snapshot_row_expression(connection: sqlite3.Connection, *, item_alias: str = "i") -> str:
    """SQL expression for immutable observed rows, including legacy databases."""
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(snapshot_members)")}
    return f"COALESCE(m.row_json, {item_alias}.row_json)" if "row_json" in columns else f"{item_alias}.row_json"
