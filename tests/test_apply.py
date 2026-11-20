"""Validation, folder inheritance, capability masking, and the guard.

The guard tests are the ones that matter most. Phase 0 confirmed that exiftool
will write an already-written file a second time without complaint, so nothing
below this layer would catch a double apply.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from photofix.apply import (
    ApprovedRow,
    ValidationError,
    build_applied_rows,
    guard,
    load_files,
    load_folders,
    parse_review_date,
    resolve_inherited,
)
from photofix.csvio import write_review_csv
from photofix.exiftool import ReadResult, normalize_key
from photofix.models import FileRecord, FolderRecord
from photofix.store import ScanStore
from photofix.walker import WalkedFile


class FakeExifTool:
    """Stands in for the binary. Returns whatever metadata the test seeded."""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self.records = records or {}
        self.calls: list[list[Path]] = []

    def read_many(self, paths: Sequence[Path]) -> ReadResult:
        self.calls.append(list(paths))
        return ReadResult(
            records={normalize_key(p): self.records.get(normalize_key(p), {}) for p in paths},
            failures=[],
        )


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2019:07:04 12:00:00", "2019:07:04 12:00:00"),
        ("2019-07-04 12:00:00", "2019:07:04 12:00:00"),
        ("2019/07/04 12:00:00", "2019:07:04 12:00:00"),
        ("2019-07-04T12:00:00", "2019:07:04 12:00:00"),
        ("2019-07-04", "2019:07:04 12:00:00"),  # date-only lands at noon
        ("2019:07:04", "2019:07:04 12:00:00"),
        ("  2019:07:04 12:00:00  ", "2019:07:04 12:00:00"),
        ("", ""),
    ],
)
def test_accepted_date_formats(raw: str, expected: str) -> None:
    assert parse_review_date(raw) == expected


@pytest.mark.parametrize("raw", ["7/4/2019", "04/07/2019", "7-4-2019", "12/31/2019 10:00:00"])
def test_ambiguous_dates_are_refused_rather_than_guessed(raw: str) -> None:
    """Guessing writes a silently wrong capture date — the bug being repaired."""
    with pytest.raises(ValidationError, match="ambiguous"):
        parse_review_date(raw)


def test_excel_serial_numbers_are_refused_with_an_explanation() -> None:
    with pytest.raises(ValidationError, match="serial number"):
        parse_review_date("43650.5")


def test_nonsense_dates_are_refused() -> None:
    with pytest.raises(ValidationError):
        parse_review_date("next tuesday")
    with pytest.raises(ValidationError):
        parse_review_date("2019:13:45 99:99:99")


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


def folders_csv(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "folders.csv"
    write_review_csv(path, FolderRecord.columns(), rows)
    return path


def folder_row(folder_path: str, title: str = "", keywords: str = "", skip: int = 0) -> dict[str, Any]:
    return {
        "folder_path": folder_path,
        "file_count": 1,
        "folder_name": Path(folder_path).name,
        "proposed_title": title,
        "proposed_keywords": keywords,
        "proposed_date": "",
        "skip": skip,
    }


def _as_folder_row(path: str, title: str, keywords: str, skip: bool = False) -> Any:
    from photofix.apply import FolderRow

    return FolderRow(folder_path=path, title=title, keywords=keywords, skip=skip)


def _folder_map(*rows: Any) -> dict[str, Any]:
    """Key the way load_folders does, on the canonical path form."""
    return {normalize_key(r.folder_path): r for r in rows}


def test_inheritance_takes_the_nearest_non_skip_ancestor() -> None:
    folders = _folder_map(
        _as_folder_row("C:/a", "Outer", "outer"),
        _as_folder_row("C:/a/b", "", "", skip=True),
        _as_folder_row("C:/a/b/c", "Inner", "inner"),
    )
    assert resolve_inherited("C:/a/b/c", folders) == ("Inner", "inner")
    assert resolve_inherited("C:/a/b", folders) == ("Outer", "outer"), "skip folders are stepped over"


def test_inheritance_survives_native_separators_in_files_csv() -> None:
    """files.csv carries native paths; folders.csv may have been written elsewhere.

    Without canonicalizing both sides, this lookup silently returns nothing and
    every file in the archive inherits no title and no keywords.
    """
    folders = _folder_map(_as_folder_row("C:/a/b", "Inner", "inner"))
    assert resolve_inherited(r"C:\a\b", folders) == ("Inner", "inner")
    assert resolve_inherited(r"C:\a\b\c", folders) == ("Inner", "inner")


def test_inheritance_returns_nothing_when_no_ancestor_offers_text() -> None:
    folders = _folder_map(_as_folder_row("C:/a", "", "", skip=True))
    assert resolve_inherited("C:/a/b", folders) == ("", "")


# ---------------------------------------------------------------------------
# Loading files.csv
# ---------------------------------------------------------------------------


def file_row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict.fromkeys(FileRecord.columns(), "")
    base.update(
        {
            "SourceFile": "C:/a/b.jpg",
            "folder_path": "C:/a",
            "filename": "b.jpg",
            "scan_size": 100,
            "scan_mtime": "1000.000000",
            "apply": 1,
            "proposed_date": "2019:07:04 12:00:00",
        }
    )
    base.update(kw)
    return base


def files_csv(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "files.csv"
    write_review_csv(path, FileRecord.columns(), rows)
    return path


def test_only_rows_marked_apply_are_loaded(tmp_path: Path) -> None:
    path = files_csv(
        tmp_path,
        [
            file_row(SourceFile="C:/a/yes.jpg", apply=1),
            file_row(SourceFile="C:/a/no.jpg", apply=0),
            file_row(SourceFile="C:/a/blank.jpg", apply=""),
        ],
    )
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a")]))
    approved, problems = load_files(path, folders)
    assert problems == []
    assert [Path(r.source_file).name for r in approved] == ["yes.jpg"]


@pytest.mark.parametrize("marker", ["1", "true", "TRUE", "yes", "y", "x"])
def test_apply_column_accepts_what_a_human_would_type(tmp_path: Path, marker: str) -> None:
    path = files_csv(tmp_path, [file_row(apply=marker)])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a")]))
    approved, _ = load_files(path, folders)
    assert len(approved) == 1


def test_duplicate_source_files_are_reported(tmp_path: Path) -> None:
    """Usually a sort-and-paste accident; applying both writes twice in one run."""
    path = files_csv(tmp_path, [file_row(), file_row()])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a")]))
    _, problems = load_files(path, folders)
    assert any("duplicate" in p for p in problems)


def test_a_bad_date_is_reported_against_its_row_number(tmp_path: Path) -> None:
    path = files_csv(tmp_path, [file_row(), file_row(SourceFile="C:/a/c.jpg", proposed_date="7/4/2019")])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a")]))
    _, problems = load_files(path, folders)
    assert len(problems) == 1
    assert "row 3" in problems[0] and "ambiguous" in problems[0]


def test_missing_columns_raise_rather_than_warn(tmp_path: Path) -> None:
    path = tmp_path / "files.csv"
    write_review_csv(path, ["SourceFile", "apply"], [{"SourceFile": "a.jpg", "apply": 1}])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a")]))
    with pytest.raises(ValidationError, match="missing required columns"):
        load_files(path, folders)


def test_file_level_values_override_folder_inheritance(tmp_path: Path) -> None:
    path = files_csv(tmp_path, [file_row(proposed_title="Chosen", proposed_keywords="mine")])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Inherited", "theirs")]))
    approved, _ = load_files(path, folders)
    assert approved[0].title == "Chosen"
    assert approved[0].keywords == "mine"


def test_blank_file_level_values_fall_back_to_the_folder(tmp_path: Path) -> None:
    path = files_csv(tmp_path, [file_row()])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Inherited", "theirs")]))
    approved, _ = load_files(path, folders)
    assert approved[0].title == "Inherited"
    assert approved[0].keywords == "theirs"


def test_a_partial_override_still_inherits_the_other_field(tmp_path: Path) -> None:
    path = files_csv(tmp_path, [file_row(proposed_title="Chosen")])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Inherited", "theirs")]))
    approved, _ = load_files(path, folders)
    assert approved[0].title == "Chosen"
    assert approved[0].keywords == "theirs"


def test_editing_folders_csv_changes_what_gets_applied(tmp_path: Path) -> None:
    """Inheritance is resolved from folders.csv, not from the scan-time columns."""
    path = files_csv(tmp_path, [file_row(inherit_title="Stale From Scan")])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Edited By Reviewer", "")]))
    approved, _ = load_files(path, folders)
    assert approved[0].title == "Edited By Reviewer"


# ---------------------------------------------------------------------------
# applied.csv construction and capability masking
# ---------------------------------------------------------------------------


def approved(source: str, **kw: Any) -> ApprovedRow:
    base: dict[str, Any] = {
        "source_file": source,
        "folder_path": str(Path(source).parent),
        "date": "2019:07:04 12:00:00",
        "title": "Trip",
        "keywords": "beach;summer",
        "scan_size": 100,
        "scan_mtime": 1000.0,
        "row_number": 2,
    }
    base.update(kw)
    return ApprovedRow(**base)


def test_jpeg_gets_every_column() -> None:
    columns, rows = build_applied_rows([approved("C:/a/b.jpg")], xmp_enabled=True)
    row = rows[0]
    assert row["EXIF:DateTimeOriginal"] == "2019:07:04 12:00:00"
    assert row["EXIF:CreateDate"] == "2019:07:04 12:00:00"
    assert row["XMP:DateCreated"] == "2019:07:04 12:00:00"
    assert row["IPTC:ObjectName"] == "Trip"
    assert row["IPTC:Keywords"] == "beach;summer"
    assert row["XMP:Title"] == "Trip"
    assert row["XMP:Subject"] == "beach;summer"


def test_gif_gets_only_xmp_columns() -> None:
    """Verified in Phase 0: GIF rejects both EXIF:DateTimeOriginal and IPTC:Keywords."""
    _, rows = build_applied_rows([approved("C:/a/b.gif")], xmp_enabled=True)
    row = rows[0]
    assert row["EXIF:DateTimeOriginal"] == ""
    assert row["IPTC:Keywords"] == ""
    assert row["XMP:DateCreated"] == "2019:07:04 12:00:00"
    assert row["XMP:Subject"] == "beach;summer"


def test_webp_gets_exif_and_xmp_but_not_iptc() -> None:
    """Also verified in Phase 0, and not something -listwf would have told us."""
    _, rows = build_applied_rows([approved("C:/a/b.webp")], xmp_enabled=True)
    row = rows[0]
    assert row["EXIF:DateTimeOriginal"] == "2019:07:04 12:00:00"
    assert row["IPTC:Keywords"] == ""
    assert row["XMP:Subject"] == "beach;summer"


def test_no_xmp_drops_the_xmp_columns_entirely() -> None:
    columns, rows = build_applied_rows([approved("C:/a/b.jpg")], xmp_enabled=False)
    assert not any(c.startswith("XMP:") for c in columns)


def test_a_gif_under_no_xmp_produces_no_row_at_all() -> None:
    """Nothing can be stored, so emitting a row would be an all-blank instruction."""
    _, rows = build_applied_rows([approved("C:/a/b.gif")], xmp_enabled=False)
    assert rows == []


def test_bmp_produces_no_row() -> None:
    _, rows = build_applied_rows([approved("C:/a/b.bmp")], xmp_enabled=True)
    assert rows == []


def test_noop_rows_are_dropped() -> None:
    _, rows = build_applied_rows([approved("C:/a/b.jpg", date="", title="", keywords="")], xmp_enabled=True)
    assert rows == []


def test_source_file_uses_forward_slashes() -> None:
    """exiftool emits and matches forward slashes on every platform."""
    _, rows = build_applied_rows([approved(r"C:\a\b.jpg")], xmp_enabled=True)
    assert rows[0]["SourceFile"] == "C:/a/b.jpg"


def test_a_date_only_row_leaves_text_columns_blank() -> None:
    _, rows = build_applied_rows([approved("C:/a/b.jpg", title="", keywords="")], xmp_enabled=True)
    assert rows[0]["IPTC:ObjectName"] == ""
    assert rows[0]["IPTC:Keywords"] == ""
    assert rows[0]["EXIF:DateTimeOriginal"] == "2019:07:04 12:00:00"


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def seeded_store(tmp_path: Path, files: list[Path]) -> ScanStore:
    store = ScanStore(tmp_path / "scan.db")
    store.upsert_walked(
        [WalkedFile(path=p, size=p.stat().st_size, mtime=p.stat().st_mtime, birthtime=None) for p in files]
    )
    return store


def test_unchanged_files_pass_the_guard(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        st = f.stat()
        rows = [approved(str(f), scan_size=st.st_size, scan_mtime=st.st_mtime)]
        outcome = guard(rows, store, FakeExifTool())
    assert len(outcome.clean) == 1
    assert outcome.blocking == []


def test_a_file_already_carrying_the_intent_is_skipped_not_rewritten(tmp_path: Path) -> None:
    """The resumed-run case: exiftool would happily write it a second time."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), scan_size=999, scan_mtime=1.0)]  # stale snapshot
        tool = FakeExifTool(
            {
                normalize_key(f): {
                    "EXIF:DateTimeOriginal": "2019:07:04 12:00:00",
                    "IPTC:ObjectName": "Trip",
                    "IPTC:Keywords": ["beach", "summer"],
                }
            }
        )
        outcome = guard(rows, store, tool)
    assert outcome.clean == []
    assert len(outcome.already_applied) == 1
    assert outcome.blocking == []


def test_a_file_changed_into_something_else_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), scan_size=999, scan_mtime=1.0)]
        tool = FakeExifTool({normalize_key(f): {"EXIF:DateTimeOriginal": "1999:01:01 00:00:00"}})
        outcome = guard(rows, store, tool)
    assert outcome.clean == []
    assert len(outcome.modified_elsewhere) == 1
    assert outcome.blocking


def test_keywords_count_as_applied_when_the_intended_ones_are_present(tmp_path: Path) -> None:
    """A merge run adds keywords; the intended set being a subset is success."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), date="", title="", scan_size=999, scan_mtime=1.0)]
        tool = FakeExifTool({normalize_key(f): {"IPTC:Keywords": ["beach", "summer", "preexisting"]}})
        outcome = guard(rows, store, tool)
    assert len(outcome.already_applied) == 1


def test_a_deleted_file_is_a_conflict_not_a_crash(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f))]
        f.unlink()
        outcome = guard(rows, store, FakeExifTool())
    assert len(outcome.missing) == 1
    assert outcome.blocking


def test_a_row_not_in_scan_db_is_refused(tmp_path: Path) -> None:
    """Catches a CSV edited to add rows, or one from a different scan."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(tmp_path / "never_scanned.jpg"))]
        with pytest.raises(ValidationError, match="not in scan.db"):
            guard(rows, store, FakeExifTool())


def test_the_guard_only_re_reads_suspect_files(tmp_path: Path) -> None:
    """Re-reading everything would double the cost of a large apply."""
    clean = tmp_path / "clean.jpg"
    clean.write_bytes(b"x" * 10)
    dirty = tmp_path / "dirty.jpg"
    dirty.write_bytes(b"y" * 10)
    with seeded_store(tmp_path, [clean, dirty]) as store:
        st = clean.stat()
        rows = [
            approved(str(clean), scan_size=st.st_size, scan_mtime=st.st_mtime),
            approved(str(dirty), scan_size=999, scan_mtime=1.0),
        ]
        tool = FakeExifTool()
        guard(rows, store, tool)
    assert len(tool.calls) == 1
    assert [p.name for p in tool.calls[0]] == ["dirty.jpg"]
