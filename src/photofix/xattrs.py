"""Extended attributes, carried across exiftool's rewrite of a file.

exiftool writes a new file and renames it over the old one, so attributes that
live on the file rather than in its metadata (Finder tags and colour labels,
"Where from", custom attributes) stay behind on `<name>_original`. Deleting the
backups once satisfied would then lose them for good.

Python's os.*xattr functions exist only on Linux; on macOS libc is called
directly. Elsewhere this is a no-op.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys
from pathlib import Path

_XATTR_NOFOLLOW = 0x0001

_libc: ctypes.CDLL | None = None
if sys.platform == "darwin":
    _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    _libc.listxattr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_int]
    _libc.listxattr.restype = ctypes.c_ssize_t
    _libc.getxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    _libc.getxattr.restype = ctypes.c_ssize_t
    _libc.setxattr.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_uint32,
        ctypes.c_int,
    ]
    _libc.setxattr.restype = ctypes.c_int


def _darwin_list(path: bytes) -> list[bytes]:
    assert _libc is not None
    size = _libc.listxattr(path, None, 0, _XATTR_NOFOLLOW)
    if size <= 0:
        return []
    buf = ctypes.create_string_buffer(size)
    size = _libc.listxattr(path, buf, size, _XATTR_NOFOLLOW)
    if size <= 0:
        return []
    return [name for name in buf.raw[:size].split(b"\0") if name]


def _darwin_get(path: bytes, name: bytes) -> bytes | None:
    assert _libc is not None
    size = _libc.getxattr(path, name, None, 0, 0, _XATTR_NOFOLLOW)
    if size < 0:
        return None
    buf = ctypes.create_string_buffer(max(size, 1))
    size = _libc.getxattr(path, name, buf, size, 0, _XATTR_NOFOLLOW)
    return None if size < 0 else buf.raw[:size]


def _darwin_set(path: bytes, name: bytes, value: bytes) -> bool:
    assert _libc is not None
    return bool(_libc.setxattr(path, name, value, len(value), 0, _XATTR_NOFOLLOW) == 0)


def read_all(path: Path) -> dict[str, bytes]:
    """Every extended attribute on `path`. Empty when there are none or it cannot tell."""
    raw = os.fsencode(path)
    out: dict[str, bytes] = {}
    if _libc is not None:
        for name in _darwin_list(raw):
            value = _darwin_get(raw, name)
            if value is not None:
                out[os.fsdecode(name)] = value
    elif sys.platform == "linux":
        try:
            for name in os.listxattr(raw, follow_symlinks=False):
                out[name] = os.getxattr(raw, name, follow_symlinks=False)
        except OSError:
            return {}
    return out


def restore(path: Path, saved: dict[str, bytes]) -> int:
    """Put back any of `saved` that `path` no longer has. Returns how many failed."""
    if not saved:
        return 0
    try:
        present = read_all(path)
    except OSError:
        return len(saved)
    raw = os.fsencode(path)
    failed = 0
    for name, value in saved.items():
        if name in present:
            continue
        if _libc is not None:
            ok = _darwin_set(raw, os.fsencode(name), value)
        elif sys.platform == "linux":
            try:
                os.setxattr(raw, name, value, follow_symlinks=False)
                ok = True
            except OSError:
                ok = False
        else:
            ok = False
        failed += not ok
    return failed
