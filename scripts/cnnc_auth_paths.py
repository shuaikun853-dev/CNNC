"""Explicit shared authentication ownership without credential copies/hardlinks."""

import os
from pathlib import Path


def get_auth_data_dir(repo_root: Path) -> Path:
    override = os.environ.get("CNNC_AUTH_DATA_DIR", "").strip()
    path = Path(override) if override else Path(repo_root) / "data"
    if not path.is_absolute():
        raise ValueError("CNNC_AUTH_DATA_DIR must be absolute")
    path = path.resolve()
    if os.name == "nt" and path.drive.upper() != "E:":
        raise ValueError("CNNC authentication data must remain on E drive")
    return path
