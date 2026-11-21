"""Walk the archive and decide what is in scope.

Skips are counted rather than silently dropped. A scan that reports "48,217
images, 3,104 videos skipped, 122 hidden skipped" is auditable; one that reports
only the first number leaves you unable to tell a correct filter from a broken
one.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .formats import FileKind, classify_extension, normalize_suffix
from .logging_setup import ProgressReporter, get_logger

#: Directory names excluded wholesale, matched case-insensitively.
EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {"__macosx", "$recycle.bin", ".trash", ".trashes", "system volume information"}
    # Synology and QNAP thumbnail caches and recycle bins.
    | {"@eadir", "#recycle", "#snapshot", "@recycle", ".@__thumb"}
)

#: Directory suffixes excluded wholesale. A .photoslibrary is an existing Apple
#: Photos database; rewriting the originals inside it would corrupt a live
#: library, and its contents are re-imported from Photos rather than from disk.
#: The same goes for iPhoto and Aperture libraries, and Lightroom's .lrdata
#: folders hold its preview renders, not photos.
EXCLUDED_DIR_SUFFIXES: Final[tuple[str, ...]] = (
    ".photoslibrary",
    ".photolibrary",
    ".aplibrary",
    ".migratedphotolibrary",
    ".lrdata",
)

#: Exact filenames excluded, matched case-insensitively.
EXCLUDED_FILENAMES: Final[frozenset[str]] = frozenset({"thumbs.db", "desktop.ini", ".ds_store", "picasa.ini"})

#: exiftool's own backups from a previous apply. Never re-scanned: they are
#: pre-change copies, so reading them would reintroduce the stale metadata the
#: tool just corrected.
ORIGINAL_SUFFIX: Final = "_original"


@dataclass(slots=True)
class WalkStats:
    images: int = 0
    videos_skipped: int = 0
    others_skipped: int = 0
    hidden_skipped: int = 0
    originals_skipped: int = 0
    excluded_dirs: int = 0
    symlinks_skipped: int = 0
    unreadable: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "images": self.images,
            "videos_skipped": self.videos_skipped,
            "others_skipped": self.others_skipped,
            "hidden_skipped": self.hidden_skipped,
            "originals_skipped": self.originals_skipped,
            "excluded_dirs": self.excluded_dirs,
            "symlinks_skipped": self.symlinks_skipped,
            "unreadable_dirs": len(self.unreadable),
        }


@dataclass(frozen=True, slots=True)
class WalkedFile:
    path: Path
    size: int
    mtime: float
    birthtime: float | None
    #: exiftool replaces a file on write, so a new inode catches a rewrite that
    #: kept both size and mtime (apply passes -P).
    inode: int = 0

    @property
    def folder(self) -> str:
        return str(self.path.parent)


def is_excluded_dir(name: str) -> bool:
    lowered = name.lower()
    if lowered in EXCLUDED_DIR_NAMES:
        return True
    if name.startswith("."):
        return True
    return lowered.endswith(EXCLUDED_DIR_SUFFIXES)


def is_excluded_file(name: str) -> bool:
    """Dotfiles, system files, and AppleDouble sidecars.

    AppleDouble files (`._IMG_1234.jpg`) are resource-fork sidecars that macOS
    scatters over non-HFS volumes. They carry the right extension and would
    otherwise be scanned as real images.
    """
    return name.startswith(".") or name.lower() in EXCLUDED_FILENAMES


def _birthtime(st: os.stat_result) -> float | None:
    """Creation time where the platform actually has one.

    macOS exposes st_birthtime. Windows puts creation time in st_ctime. Linux's
    st_ctime is the inode *change* time, which is not a creation time at all and
    is usually the moment the file was copied onto the disk — so it is refused
    rather than fed to the date proposer as if it meant something.
    """
    birth = getattr(st, "st_birthtime", None)
    if birth is not None:
        return float(birth)
    if sys.platform == "win32":
        return float(st.st_ctime)
    return None


def walk_images(
    root: Path,
    *,
    limit: int | None = None,
    stats: WalkStats | None = None,
    report_progress: bool = True,
) -> Iterator[WalkedFile]:
    """Yield in-scope image files beneath `root`, deepest-first ordering aside.

    `limit` stops the walk early for rehearsals. It caps *images yielded*, not
    files examined, so `--limit 100` always produces 100 usable rows rather than
    100 directory entries that might all be videos.
    """
    stats = stats if stats is not None else WalkStats()
    log = get_logger()
    progress = ProgressReporter("walk", interval_seconds=5.0) if report_progress else None
    yielded = 0

    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: _on_error(e, stats, log)):
        kept = []
        for name in dirnames:
            if is_excluded_dir(name):
                stats.excluded_dirs += 1
            else:
                kept.append(name)
        # In-place so os.walk does not descend into what we just excluded.
        dirnames[:] = kept

        for name in sorted(filenames):
            if is_excluded_file(name):
                stats.hidden_skipped += 1
                continue
            if name.endswith(ORIGINAL_SUFFIX):
                stats.originals_skipped += 1
                continue

            kind = classify_extension(name)
            if kind is FileKind.VIDEO:
                stats.videos_skipped += 1
                continue
            if kind is not FileKind.IMAGE:
                stats.others_skipped += 1
                continue

            path = Path(dirpath) / name
            if path.is_symlink():
                # exiftool writes a new file in the link's place, which would
                # turn the link into a full copy and leave the target untouched.
                stats.symlinks_skipped += 1
                continue
            try:
                st = path.stat()
            except OSError as exc:
                stats.unreadable.append(str(path))
                log.warning("cannot stat file", extra={"path": str(path), "error": str(exc)})
                continue

            stats.images += 1
            yielded += 1
            if progress:
                progress.advance(context=dirpath)
            yield WalkedFile(
                path=path, size=st.st_size, mtime=st.st_mtime, birthtime=_birthtime(st), inode=st.st_ino
            )

            if limit is not None and yielded >= limit:
                if progress:
                    progress.finish(context=f"stopped at --limit {limit}")
                log.info("walk stopped early", extra={"limit": limit, **stats.as_dict()})
                return

    if progress:
        progress.finish()
    log.info("walk complete", extra=stats.as_dict())


def _on_error(exc: OSError, stats: WalkStats, log: object) -> None:
    stats.unreadable.append(str(getattr(exc, "filename", "?")))
    get_logger().warning(
        "cannot read directory",
        extra={"path": str(getattr(exc, "filename", "?")), "error": str(exc)},
    )


def is_supported_image(path: Path | str) -> bool:
    name = path.name if isinstance(path, Path) else str(path)
    return (
        not is_excluded_file(name)
        and not name.endswith(ORIGINAL_SUFFIX)
        and classify_extension(name) is FileKind.IMAGE
        and normalize_suffix(name) != ""
    )
