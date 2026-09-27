"""Non-following filesystem guards supported by Python 3.11 and later."""

from pathlib import Path
import stat


def is_reparse_point(path: Path) -> bool:
    """Treat symlinks, all Windows reparse points and unreadable entries as unsafe."""
    try:
        metadata = Path(path).lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )
