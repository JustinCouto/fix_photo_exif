"""Walk filtering, and the resume store's invalidation rule.

The invalidation test is the important one here. Stale cached metadata does not
announce itself — it produces a scan that looks complete and an apply phase that
compares files against a snapshot that no longer describes them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from photofix.exiftool import argfile_line
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
        ("@eaDir", True),
        ("#recycle", True),
        ("iPhoto Library.photolibrary", True),
        ("Lightroom Catalog Previews.lrdata", True),
        ("Vacation 2019", False),
    ],
)
def test_excluded_dirs(name: str, excluded: bool) -> None:
    assert is_excluded_dir(name) is excluded


def test_symlinked_files_are_skipped(tmp_path: Path) -> None:
    """exiftool would replace the link with a full copy and leave the target as it was."""
    (tmp_path / "real.jpg").write_bytes(b"a")
    (tmp_path / "link.jpg").symlink_to(tmp_path / "real.jpg")
    stats = WalkStats()
    names = [f.path.name for f in walk_images(tmp_path, stats=stats, report_progress=False)]
    assert names == ["real.jpg"]
    assert stats.symlinks_skipped == 1


@pytest.mark.parametrize(
    "raw,normalized",
    [
        ("2015:06:20", "2015:06:20 12:00:00"),
        ("2015:06:20+02:00", "2015:06:20 12:00:00"),
        ("2015:06:20 14:30:00-07:00", "2015:06:20 14:30:00"),
    ],
)
def test_date_only_timestamps_are_real_dates(raw: str, normalized: str) -> None:
    assert normalize_timestamp(raw) == normalized


def _read(store: ScanStore, path: Path, meta: dict[str, object]) -> None:
    store.upsert_walked([walked(path)])
    store.record_metadata({str(path).replace("\\", "/"): {"Photofix:IPTCRead": True, **meta}})


def test_an_existing_xmp_or_iptc_date_counts_as_the_photos_date(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        _read(store, tmp_path / "a.jpg", {"XMP:DateCreated": "2015:06:20 14:30:00"})
        _read(store, tmp_path / "b.jpg", {"IPTC:DateCreated": "2016:07:04"})
        assert store.get(str(tmp_path / "a.jpg")).datetime_original == "2015:06:20 14:30:00"  # type: ignore[union-attr]
        assert store.get(str(tmp_path / "b.jpg")).datetime_original == "2016:07:04"  # type: ignore[union-attr]


def test_other_date_tags_are_available_when_the_first_is_bad(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        meta: dict[str, object] = {
            "EXIF:DateTimeOriginal": "2002:12:08 12:00:00",
            "IPTC:DateCreated": "1984:12:25",
        }
        _read(store, tmp_path / "a.jpg", meta)
        got = store.get(str(tmp_path / "a.jpg"))
        assert got is not None
        assert got.other_dates == [("IPTC:DateCreated", "1984:12:25")]


def test_argfile_lines_escape_newlines_in_paths() -> None:
    """Unescaped, "x\\n-Title=y.jpg" would hand exiftool a -Title= option."""
    assert argfile_line("/a/b c.jpg") == "/a/b c.jpg"
    assert argfile_line("/a/x\n-Title=y.jpg") == "#[CSTR]/a/x\\n-Title=y.jpg"
    assert argfile_line("/a\\b/x\n.jpg") == "#[CSTR]/a\\\\b/x\\n.jpg"


def test_a_placeholder_title_does_not_hide_a_real_one(tmp_path: Path) -> None:
    with ScanStore(tmp_path / "scan.db") as store:
        _read(store, tmp_path / "a.jpg", {"XMP:Title": "Untitled", "IPTC:ObjectName": "Grandma at the lake"})
        assert store.get(str(tmp_path / "a.jpg")).title == "Grandma at the lake"  # type: ignore[union-attr]


def test_unreadable_files_are_retried_on_the_next_scan(tmp_path: Path) -> None:
    p = tmp_path / "a.jpg"
    with ScanStore(tmp_path / "scan.db") as store:
        store.upsert_walked([walked(p)])
        store.mark_unreadable([str(p)], "exiftool could not read")
        assert store.count(pending_only=True) == 0
        assert store.retry_unreadable() == 1
        assert store.count(pending_only=True) == 1


def test_prune_forgets_files_the_walk_no_longer_found(tmp_path: Path) -> None:
    kept, gone = tmp_path / "kept.jpg", tmp_path / "gone.jpg"
    with ScanStore(tmp_path / "scan.db") as store:
        store.upsert_walked([walked(kept), walked(gone)])
        assert store.prune({str(kept).replace("\\", "/")}) == 1
        assert [f.filename for f in store.iter_files()] == ["kept.jpg"]


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


def test_a_rewrite_that_kept_size_and_mtime_is_caught_by_its_inode(tmp_path: Path) -> None:
    """apply passes -P, so only the new inode shows exiftool replaced the file."""
    p = tmp_path / "a.jpg"
    key = str(p).replace("\\", "/")
    with ScanStore(tmp_path / "scan.db") as store:
        store.upsert_walked([WalkedFile(path=p, size=10, mtime=1000.0, birthtime=None, inode=1)])
        store.record_metadata({key: {"EXIF:DateTimeOriginal": "2000:01:01 00:00:00"}})
        store.upsert_walked([WalkedFile(path=p, size=10, mtime=1000.0, birthtime=None, inode=2)])
        assert store.count(pending_only=True) == 1


def test_a_scan_db_from_before_inodes_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "scan.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE files (path TEXT PRIMARY KEY, real_path TEXT NOT NULL, folder TEXT NOT NULL, "
        "filename TEXT NOT NULL, ext TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL, "
        "birthtime REAL, meta_json TEXT, scanned_at REAL, datetime_original TEXT, "
        "datetime_original_norm TEXT, create_date TEXT, keywords TEXT, title TEXT, mime_type TEXT, "
        "warning TEXT, error TEXT)"
    )
    conn.commit()
    conn.close()
    with ScanStore(db) as store:
        store.upsert_walked([walked(tmp_path / "a.jpg")])
        assert store.count(pending_only=True) == 1


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
