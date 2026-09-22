"""One write-path policy shared by offline recovery and manifest tools."""
from pathlib import Path, PureWindowsPath
import os
import ntpath
import re




def _windows_drive_info(drive: str) -> tuple[int, str]:
    """Read volume metadata only; do not traverse a mapped share."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_type = kernel.GetDriveTypeW
    get_type.argtypes = [wintypes.LPCWSTR]
    get_type.restype = wintypes.UINT
    query = kernel.QueryDosDeviceW
    query.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    query.restype = wintypes.DWORD
    kind = int(get_type(drive + chr(92)))
    buffer = ctypes.create_unicode_buffer(32768)
    if not query(drive, buffer, len(buffer)):
        raise ValueError('Cannot establish the output drive identity')
    return kind, buffer.value


def _check_backing_volume(drive: str) -> None:
    if os.name != 'nt' or not drive:
        return
    _, c_target = _windows_drive_info('C:')
    visited = set()
    for _ in range(8):
        if drive.casefold() == 'c:' or drive.casefold() in visited:
            raise ValueError('C-drive aliases and cyclic drive aliases are forbidden')
        visited.add(drive.casefold())
        kind, target = _windows_drive_info(drive)
        if kind in {0, 1, 4}:
            raise ValueError('Unknown and network output volumes are forbidden')
        if target.casefold() == c_target.casefold() or target.casefold().startswith(c_target.casefold().rstrip(chr(92)) + chr(92)):
            raise ValueError('Output drive is backed by the protected C volume')
        # QueryDosDevice exposes SUBST as a DOS device target (backslash ?? backslash).
        prefix = chr(92) + '??' + chr(92)
        if target.startswith(prefix) and len(target) >= len(prefix) + 2:
            next_drive = target[len(prefix):len(prefix)+2]
            if re.fullmatch('[A-Za-z]:', next_drive):
                drive = next_drive
                continue
        return
    raise ValueError('Too many nested drive aliases')

def _ordinary_local_path(value: Path | str) -> None:
    drive = PureWindowsPath(str(value)).drive
    # Reject UNC and Windows device spellings before resolving/accessing them.
    # Callers must use an ordinary local path, such as E:/audit/output.
    if drive.startswith(chr(92)) or str(value).startswith(('//', chr(92) * 2)):
        raise ValueError('Network and device paths are not supported for audit outputs')
    # Reject ADS, reserved devices and normalized-away suffixes before any I/O.
    if ntpath.isreserved(str(value)):
        raise ValueError('Reserved Windows names and alternate data streams are forbidden')
    if drive.casefold() == 'c:':
        raise ValueError('C drive is read-only for this project')
    _check_backing_volume(drive)


def new_local_output_path(value: Path | str) -> Path:
    """Validate a new output, without writing or creating any parent folder."""
    _ordinary_local_path(value)
    lexical = Path(value).absolute()
    resolved = lexical.resolve()
    for path in (lexical, resolved):
        _ordinary_local_path(path)
        if 'cnnc-prod-runtime' in {part.casefold() for part in path.parts}:
            raise ValueError('Writing inside the live production checkout is prohibited')
    if lexical.exists() or lexical.is_symlink() or getattr(lexical, 'is_junction', lambda: False)():
        raise FileExistsError('Destination must not exist')
    return resolved
