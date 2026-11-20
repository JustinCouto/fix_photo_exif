"""Walk filtering, and the resume store's invalidation rule.

The invalidation test is the important one here. Stale cached metadata does not
announce itself — it produces a scan that looks complete and an apply phase that
compares files against a snapshot that no longer describes them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photofix.store import ScanStore, normalize_timestamp
from photofix.walker import (
    WalkedFile,
    WalkStats,
    is_excluded_dir,
    is_excluded_file,
    walk_images,
)


def make_tree(root: Path) -> None:
    (root / "album").mkdir(parents=True)
    (root / "album" / "a.jpg").write_bytes(b"a")
    (root / "album" / "b.HEIC").write_bytes(b"b")
    (root / "album" / "clip.mov").write_bytes(b"v")
    (root / "album" / "notes.txt").write_bytes(b"t")
    (root / "album" / "Thumbs.db").write_bytes(b"x")
    (root / "album" / ".DS_Store").write_bytes(b"x")
    (root / "album" / "._a.jpg").write_bytes(b"x")  # AppleDouble sidecar
    (root / "album" / "a.jpg_original").write_bytes(b"x")  # previous apply's backup

    (root / "__MACOSX").mkdir()
    (root / "__MACOSX" / "c.jpg").write_bytes(b"c")
    (root / ".hidden").mkdir()
    (root / ".hidden" / "d.jpg").write_bytes(b"d")
    (root / "Old.photoslibrary").mkdir()
    (root / "Old.photoslibrary" / "e.jpg").write_bytes(b"e")


def test_walk_selects_only_in_scope_images(tmp_path: Path) -> None:
    make_tree(tmp_path)
    stats = WalkStats()
    found = sorted(f.path.name for f in walk_images(tmp_path, stats=stats, report_progress=False))
    assert found == ["a.jpg", "b.HEIC"]
    assert stats.images == 2
    assert stats.videos_skipped == 1
    assert stats.others_skipped == 1  # notes.txt
    assert stats.originals_skipped == 1  # a.jpg_original
    assert stats.hidden_skipped == 3  # Thumbs.db, .DS_Store, ._a.jpg


def test_walk_never_descends_into_a_photos_library(tmp_path: Path) -> None:
    """Rewriting originals inside a live Apple Photos library would corrupt it."""
    make_tree(tmp_path)
    names = [f.path.name for f in walk_images(tmp_path, report_progress=False)]
    assert "e.jpg" not in names


def test_limit_caps_images_yielded_not_files_examined(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    for i in range(10):
        (tmp_path / "d" / f"v{i}.mov").write_bytes(b"v")
    for i in range(5):
        (tmp_path / "d" / f"p{i}.jpg").write_bytes(b"p")

    got = list(walk_images(tmp_path, limit=3, report_progress=False))
    assert len(got) == 3, "--limit 3 must yield 3 usable images, not stop after 3 entries"


@pytest.mark.parametrize(
    "name,excluded",
    [
        ("__MACOSX", True),
        (".hidden", True),
        ("My.photoslibrary", True),
        ("$RECYCLE.BIN", True),
        ("Vacation 2019", False),
    ],
)
def test_excluded_dirs(name: str, excluded: bool) -> None:
    assert is_excluded_dir(name) is excluded


@pytest.mark.parametrize(
    "name,excluded",
    [
        ("Thumbs.db", True),
        ("desktop.ini", True),
        (".DS_Store", True),
        ("._IMG_1234.jpg", True),
        ("IMG_1234.jpg", False),
    ],
)
def test_excluded_files(name: str, excluded: bool) -> None:
    assert is_excluded_file(name) is excluded


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def walked(path: Path, size: int = 10, mtime: float = 1000.0) -> WalkedFile:
    return WalkedFile(path=path, size=size, mtime=mtime, birthtime=900.0)


def test_pending_shrinks_as_metadata_arrives(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        p = tmp_path / "a.jpg"
        store.upsert_walked([walked(p)])
        assert store.count(pending_only=True) == 1

        key = store.pending_paths()
        assert key == [p]

        store.record_metadata({str(p).replace("\\", "/"): {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00"}})
        assert store.count(pending_only=True) == 0


def test_resume_only_reads_what_is_still_pending(tmp_path: Path) -> None:
    db = tmp_path / "scan.db"
    files = [tmp_path / f"{i}.jpg" for i in range(5)]
    with ScanStore(db) as store:
        store.upsert_walked([walked(p) for p in files])
        # Simulate an interrupt after three of five files were read.
        store.record_metadata(
            {str(p).replace("\\", "/"): {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00"} for p in files[:3]}
        )

    with ScanStore(db) as store:
        assert sorted(p.name for p in store.pending_paths()) == ["3.jpg", "4.jpg"]


def test_changed_file_discards_its_cached_metadata(tmp_path: Path) -> None:
    """The stale-snapshot trap: a re-walk must invalidate what it invalidates.

    Without the CASE guards in upsert_walked, an edited file keeps metadata read
    before the edit, and apply would later compare it against a snapshot that
    never described it.
    """
    db = tmp_path / "scan.db"
    p = tmp_path / "a.jpg"
    key = str(p).replace("\\", "/")

    with ScanStore(db) as store:
        store.upsert_walked([walked(p, size=10, mtime=1000.0)])
        store.record_metadata({key: {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00"}})
        assert store.count(pending_only=True) == 0

        # Same path, different size: the file changed under us.
        store.upsert_walked([walked(p, size=99, mtime=2000.0)])
        assert store.count(pending_only=True) == 1, "changed file must be re-read"

        stored = store.get(key)
        assert stored is not None
        assert stored.datetime_original is None, "extracted columns must be cleared too"
        assert stored.size == 99 and stored.mtime == 2000.0


def test_unchanged_file_keeps_its_cached_metadata(tmp_path: Path) -> None:
    """The other half: a re-walk must not throw away work it does not need to."""
    db = tmp_path / "scan.db"
    p = tmp_path / "a.jpg"
    key = str(p).replace("\\", "/")
    with ScanStore(db) as store:
        store.upsert_walked([walked(p, size=10, mtime=1000.0)])
        store.record_metadata({key: {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00"}})
        store.upsert_walked([walked(p, size=10, mtime=1000.0)])
        assert store.count(pending_only=True) == 0
        stored = store.get(key)
        assert stored is not None and stored.datetime_original == "2019:07:04 12:00:00"


def test_clusters_group_on_normalized_timestamp(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        paths = [tmp_path / "f1" / f"a{i}.jpg" for i in range(15)]
        paths += [tmp_path / "f2" / f"b{i}.jpg" for i in range(10)]
        store.upsert_walked([walked(p) for p in paths])
        # Subsecond and timezone variants must land in the same bucket.
        store.record_metadata(
            {
                str(p).replace("\\", "/"): {
                    "EXIF:DateTimeOriginal": "2013:01:01 00:00:00" + (".00" if i % 2 else "")
                }
                for i, p in enumerate(paths)
            }
        )
        clusters = store.clusters(threshold=20)
        assert "2013:01:01 00:00:00" in clusters
        found = clusters["2013:01:01 00:00:00"]
        assert found.size == 25
        assert found.folders == 2, "folder spread separates a camera burst from a bulk edit"


def test_cluster_threshold_is_inclusive(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        paths = [tmp_path / f"a{i}.jpg" for i in range(19)]
        store.upsert_walked([walked(p) for p in paths])
        store.record_metadata(
            {str(p).replace("\\", "/"): {"EXIF:DateTimeOriginal": "2013:01:01 00:00:00"} for p in paths}
        )
        assert store.clusters(threshold=20) == {}

        store.upsert_walked([walked(tmp_path / "a19.jpg")])
        store.record_metadata(
            {str(tmp_path / "a19.jpg").replace("\\", "/"): {"EXIF:DateTimeOriginal": "2013:01:01 00:00:00"}}
        )
        assert len(store.clusters(threshold=20)) == 1


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2019:07:04 12:00:00", "2019:07:04 12:00:00"),
        ("2019:07:04 12:00:00.00", "2019:07:04 12:00:00"),
        ("2019:07:04 12:00:00+01:00", "2019:07:04 12:00:00"),
        ("0000:00:00 00:00:00", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_timestamp(raw: str | None, expected: str | None) -> None:
    assert normalize_timestamp(raw) == expected


def test_keywords_and_title_are_extracted_from_group_qualified_keys(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        p = tmp_path / "a.jpg"
        key = str(p).replace("\\", "/")
        store.upsert_walked([walked(p)])
        store.record_metadata({key: {"IPTC:Keywords": ["beach", "summer"], "IPTC:ObjectName": "Trip"}})
        stored = store.get(key)
        assert stored is not None
        assert stored.keywords == "beach;summer"
        assert stored.title == "Trip"
