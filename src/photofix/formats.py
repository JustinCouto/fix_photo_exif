"""Extension classification and per-format write capability.

The capability matrix is empirically derived, not assumed. `exiftool -listwf`
reports writability per *file type*, which is not the same question as whether a
given format accepts a given tag — `tools/phase0_probe.py` answers the latter by
writing to scratch files and reading back what landed.

Verified on exiftool 13.59:

    .png   EXIF yes   IPTC yes(*)  XMP yes
    .webp  EXIF yes   IPTC NO      XMP yes
    .gif   EXIF NO    IPTC NO      XMP yes
    .bmp   not writable at all

(*) PNG accepts IPTC but exiftool warns "Creating non-standard IPTC in PNG". We
decline it: XMP reaches PNG cleanly and Apple Photos reads XMP, so there is
nothing to gain from writing a structure the format does not standardly carry.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final, Literal

TagFamily = Literal["EXIF", "IPTC", "XMP"]


class FileKind(StrEnum):
    """What the walker decided a path is."""

    IMAGE = "image"
    VIDEO = "video"
    OTHER = "other"


IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".jpg",
        ".jpeg",
        ".png",
        ".heic",
        ".heif",
        ".tif",
        ".tiff",
        ".gif",
        ".bmp",
        ".webp",
        ".dng",
        ".cr2",
        ".nef",
        ".arw",
        ".raf",
        ".orf",
    }
)

# Skipped entirely and only counted. Video is a separate pass by design.
VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {
        ".mov",
        ".mp4",
        ".m4v",
        ".avi",
        ".wmv",
        ".3gp",
        ".mpg",
        ".mpeg",
        ".mts",
        ".mkv",
    }
)

_ALL: Final[frozenset[TagFamily]] = frozenset({"EXIF", "IPTC", "XMP"})
_EXIF_XMP: Final[frozenset[TagFamily]] = frozenset({"EXIF", "XMP"})
_XMP_ONLY: Final[frozenset[TagFamily]] = frozenset({"XMP"})
_NONE: Final[frozenset[TagFamily]] = frozenset()

#: Which tag families each extension can actually store.
WRITE_CAPABILITY: Final[dict[str, frozenset[TagFamily]]] = {
    # Full EXIF + IPTC + XMP.
    ".jpg": _ALL,
    ".jpeg": _ALL,
    ".tif": _ALL,
    ".tiff": _ALL,
    ".heic": _ALL,
    ".heif": _ALL,
    # TIFF-based RAW. All six are in `exiftool -listwf`; an earlier draft of the
    # plan called .arw/.raf/.orf "limited", which was wrong about write support.
    # The real caution is maker-note preservation, which _original backups cover.
    ".dng": _ALL,
    ".cr2": _ALL,
    ".nef": _ALL,
    ".arw": _ALL,
    ".raf": _ALL,
    ".orf": _ALL,
    # Verified: EXIF and XMP land, IPTC does not (PNG's IPTC is non-standard).
    ".png": _EXIF_XMP,
    ".webp": _EXIF_XMP,
    # Verified: GIF has no EXIF container and rejects IPTC. Its date must go to
    # XMP:DateCreated or nowhere.
    ".gif": _XMP_ONLY,
    # Absent from `exiftool -listwf`. Read-only, never written.
    ".bmp": _NONE,
}


def normalize_suffix(path: Path | str) -> str:
    """Lowercased extension including the dot, or "" when there is none."""
    name = path.name if isinstance(path, Path) else str(path)
    _, dot, ext = name.rpartition(".")
    return f".{ext.lower()}" if dot else ""


def classify_extension(path: Path | str) -> FileKind:
    suffix = normalize_suffix(path)
    if suffix in IMAGE_EXTENSIONS:
        return FileKind.IMAGE
    if suffix in VIDEO_EXTENSIONS:
        return FileKind.VIDEO
    return FileKind.OTHER


def writable_families(path: Path | str, *, xmp_enabled: bool = True) -> frozenset[TagFamily]:
    """Tag families that can be written to this path, honoring --no-xmp.

    Under ``--no-xmp`` a .gif has nowhere left to put anything, which is why the
    apply phase re-evaluates capability against its own flags instead of trusting
    the scan-time ``writable`` column.
    """
    families = WRITE_CAPABILITY.get(normalize_suffix(path), _NONE)
    if not xmp_enabled:
        families = frozenset(f for f in families if f != "XMP")
    return families


def can_write_date(path: Path | str, *, xmp_enabled: bool = True) -> bool:
    return bool(writable_families(path, xmp_enabled=xmp_enabled))


def can_write_text(path: Path | str, *, xmp_enabled: bool = True) -> bool:
    """Whether keywords/title have anywhere to live."""
    families = writable_families(path, xmp_enabled=xmp_enabled)
    return "IPTC" in families or "XMP" in families


def is_writable(path: Path | str, *, xmp_enabled: bool = True) -> bool:
    """The ``writable`` column: 0 only when no proposed tag can be stored at all."""
    return bool(writable_families(path, xmp_enabled=xmp_enabled))
