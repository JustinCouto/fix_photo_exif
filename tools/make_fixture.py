"""Build a scratch archive that exercises the interesting cases.

Used for rehearsal and by the integration test. Writes real images with real
(and deliberately wrong) metadata, so the tool is exercised end to end rather
than against mocks.

    uv run python tools/make_fixture.py .scratch/archive
"""

from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
WEBP = base64.b64decode("UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAwA0JaQAA3AA/vuUAAA=")

#: A 1x1 24-bit BMP: file header, BITMAPINFOHEADER, one padded pixel row.
BMP = (
    b"BM"
    + (58).to_bytes(4, "little")
    + b"\x00" * 4
    + (54).to_bytes(4, "little")
    + (40).to_bytes(4, "little")
    + (1).to_bytes(4, "little")
    + (1).to_bytes(4, "little")
    + (1).to_bytes(2, "little")
    + (24).to_bytes(2, "little")
    + b"\x00" * 4
    + (4).to_bytes(4, "little")
    + b"\x00" * 16
    + b"\xff\xff\xff\x00"
)

SEEDS = {".jpg": JPEG, ".png": PNG, ".gif": GIF, ".webp": WEBP, ".bmp": BMP}


def make(root: Path) -> list[tuple[Path, str | None]]:
    """Create the tree. Returns (path, date-to-stamp-or-None) pairs."""
    plan: list[tuple[Path, str | None]] = []

    def add(rel: str, date: str | None = None) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(SEEDS[path.suffix.lower()])
        plan.append((path, date))

    # A well-named folder: title and keywords should be inherited.
    add("Hawaii Trip 2019/IMG_0001.jpg")  # missing date, folder-dated -> medium
    add("Hawaii Trip 2019/2019-07-04 sunset.jpg")  # filename date -> high
    add("Hawaii Trip 2019/DSC_0704.jpg")  # camera sequence: must NOT parse
    add("Hawaii Trip 2019/good.jpg", "2019:07:04 15:00:00")  # already ok

    # A junk folder name that is still a usable date source.
    add("2020-01-15/IMG_0002.jpg")
    add("2020-01-15/PXL_20220101_123456789.jpg")

    # A device dump with nothing to inherit.
    add("DCIM/100CANON/IMG_1234.jpg")

    # Implausible dates.
    add("Old Scans/scan01.jpg", "2002:12:08 12:00:00")  # scanner default
    add("Old Scans/scan02.jpg", "1980:01:01 00:00:00")  # firmware default

    # A cluster: 25 files sharing one timestamp across two folders.
    for i in range(15):
        add(f"Bulk Export/a{i:03d}.jpg", "2013:01:01 00:00:00")
    for i in range(10):
        add(f"Bulk Export 2/b{i:03d}.jpg", "2013:01:01 00:00:00")

    # Formats with partial or no write capability.
    add("Mixed Formats/animation_2019-07-04.gif")  # XMP only
    add("Mixed Formats/sticker.webp")  # no IPTC
    add("Mixed Formats/diagram.png")
    add("Mixed Formats/old.bmp")  # unwritable

    # Accented names and values.
    add("Café Zürich Noël/IMG_0003.jpg")

    # Things that must be skipped.
    (root / "Hawaii Trip 2019" / "Thumbs.db").write_bytes(b"x")
    (root / "Hawaii Trip 2019" / ".DS_Store").write_bytes(b"x")
    (root / "Hawaii Trip 2019" / "._IMG_0001.jpg").write_bytes(b"x")
    (root / "Hawaii Trip 2019" / "clip.mov").write_bytes(b"v")
    (root / "__MACOSX").mkdir(exist_ok=True)
    (root / "__MACOSX" / "junk.jpg").write_bytes(JPEG)
    (root / "Existing.photoslibrary").mkdir(exist_ok=True)
    (root / "Existing.photoslibrary" / "inside.jpg").write_bytes(JPEG)

    return plan


def stamp(plan: list[tuple[Path, str | None]], exiftool: str = "exiftool") -> None:
    """Write the seeded dates in one invocation."""
    dated = [(p, d) for p, d in plan if d]
    if not dated:
        return
    for path, date in dated:
        subprocess.run(
            [
                exiftool,
                f"-EXIF:DateTimeOriginal={date}",
                "-overwrite_original",  # fixture setup only, never in the tool itself
                "-q",
                str(path),
            ],
            check=False,
            capture_output=True,
        )


def build(root: Path, *, exiftool: str = "exiftool") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    plan = make(root)
    stamp(plan, exiftool)
    return root


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".scratch/archive")
    built = build(target)
    print(f"fixture archive at {built.resolve()}")
