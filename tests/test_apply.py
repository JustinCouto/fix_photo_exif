"""Validation, folder inheritance, capability masking, and the guard.

The guard tests are the ones that matter most. Phase 0 confirmed that exiftool
will write an already-written file a second time without complaint, so nothing
below this layer would catch a double apply.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from photofix.apply import (
    ApprovedRow,
    ValidationError,
    build_applied_rows,
    check_disk_space,
    fit_iptc,
    guard,
    iptc_charset,
    load_files,
    load_folders,
    parse_review_date,
    record_applied,
    resolve_inherited,
    write_blocker,
)
from photofix.csvio import CsvEncodingError, read_review_csv, write_review_csv
from photofix.exiftool import (
    DELETE_MARKER,
    READ_VERSION,
    READ_VERSION_TAG,
    ReadResult,
    normalize_key,
    parse_update_counts,
)
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
        "title": title,
        "keywords": keywords,
        "date_found": "",
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


@pytest.mark.skipif(os.sep != "\\", reason="backslash is a filename character off Windows")
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
    path = files_csv(tmp_path, [file_row(override_title="Chosen", override_keywords="mine")])
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
    path = files_csv(tmp_path, [file_row(override_title="Chosen")])
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Inherited", "theirs")]))
    approved, _ = load_files(path, folders)
    assert approved[0].title == "Chosen"
    assert approved[0].keywords == "theirs"


def test_a_filename_title_beats_the_folder_title_but_not_an_override(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Folder Title", "theirs")]))

    approved, _ = load_files(files_csv(tmp_path, [file_row(filename_title="From Name")]), folders)
    assert approved[0].title == "From Name"
    assert approved[0].keywords == "theirs"

    approved, _ = load_files(
        files_csv(tmp_path, [file_row(filename_title="From Name", override_title="Chosen")]), folders
    )
    assert approved[0].title == "Chosen"


def test_clearing_filename_title_falls_back_to_the_folder(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Folder Title", "")]))
    approved, _ = load_files(files_csv(tmp_path, [file_row(filename_title="")]), folders)
    assert approved[0].title == "Folder Title"


def test_an_existing_title_and_keywords_are_never_replaced_by_derived_ones(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Folder Title", "theirs")]))
    row = file_row(filename_title="From Name", existing_title="Grandma's 80th", existing_keywords="family")
    approved, _ = load_files(files_csv(tmp_path, [row]), folders)
    assert approved[0].title == ""
    assert approved[0].keywords == ""
    assert approved[0].date, "the date fix still goes through"


def test_a_typed_override_does_replace_an_existing_title(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Folder Title", "")]))
    row = file_row(override_title="Chosen", existing_title="Grandma's 80th")
    approved, _ = load_files(files_csv(tmp_path, [row]), folders)
    assert approved[0].title == "Chosen"


def test_a_placeholder_existing_title_is_replaced(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "Folder Title", "")]))
    row = file_row(existing_title="OLYMPUS DIGITAL CAMERA")
    approved, _ = load_files(files_csv(tmp_path, [row]), folders)
    assert approved[0].title == "Folder Title"


def test_merge_keywords_adds_folder_keywords_beside_existing_ones(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a", "", "theirs")]))
    row = file_row(existing_keywords="family")
    approved, _ = load_files(files_csv(tmp_path, [row]), folders, merge_keywords=True)
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


@pytest.mark.skipif(os.sep != "\\", reason="Windows paths only")
def test_source_file_uses_forward_slashes() -> None:
    """exiftool emits and matches forward slashes on Windows."""
    _, rows = build_applied_rows([approved(r"C:\a\b.jpg")], xmp_enabled=True)
    assert rows[0]["SourceFile"] == "C:/a/b.jpg"


@pytest.mark.skipif(os.sep == "\\", reason="a backslash is a separator on Windows")
def test_backslash_in_a_filename_is_kept() -> None:
    """On macOS "a\\b.jpg" is one file; turning it into "a/b.jpg" names another."""
    _, rows = build_applied_rows([approved("/x/a\\b.jpg")], xmp_enabled=True)
    assert rows[0]["SourceFile"] == "/x/a\\b.jpg"
    assert normalize_key("/x/a\\b.jpg") == "/x/a\\b.jpg"


def test_a_date_only_row_leaves_text_columns_blank() -> None:
    _, rows = build_applied_rows([approved("C:/a/b.jpg", title="", keywords="")], xmp_enabled=True)
    assert rows[0]["IPTC:ObjectName"] == ""
    assert rows[0]["IPTC:Keywords"] == ""
    assert rows[0]["EXIF:DateTimeOriginal"] == "2019:07:04 12:00:00"


LONG_TITLE = "Barney opening presents on Christmas morning at Grandma and Grandpa's house"


def test_iptc_values_are_cut_to_64_bytes_at_a_word_while_xmp_keeps_everything() -> None:
    long_keyword = "Grandma and Grandpa's house on the lake in northern Minnesota woods"
    _, rows = build_applied_rows(
        [approved("C:/a/b.jpg", title=LONG_TITLE, keywords=f"beach;{long_keyword}")], xmp_enabled=True
    )
    assert rows[0]["IPTC:ObjectName"] == "Barney opening presents on Christmas morning at Grandma and"
    assert rows[0]["XMP:Title"] == LONG_TITLE
    assert rows[0]["IPTC:Keywords"] == "beach;Grandma and Grandpa's house on the lake in northern Minnesota"
    assert rows[0]["XMP:Subject"] == f"beach;{long_keyword}"


@pytest.mark.parametrize(
    "meta,charset",
    [
        ({"Photofix:IPTCRead": True}, "none"),
        ({"Photofix:IPTCRead": True, "IPTC:CodedCharacterSet": "UTF8", "IPTC:Keywords": ["a"]}, "utf8"),
        ({"Photofix:IPTCRead": True, "IPTC:Caption-Abstract": "Café"}, "legacy"),
        ({}, "legacy"),  # read before IPTC was recorded: assume the cautious case
    ],
)
def test_iptc_charset(meta: dict[str, Any], charset: str) -> None:
    assert iptc_charset(meta) == charset


def test_utf8_is_declared_only_where_it_cannot_relabel_existing_iptc() -> None:
    rows = [approved("C:/a/new.jpg", title="Noël"), approved("C:/a/old.jpg", title="Noël")]
    charsets: dict[str, Any] = {"C:/a/new.jpg": "none", "C:/a/old.jpg": "legacy"}
    _, out = build_applied_rows(rows, xmp_enabled=True, iptc_charsets=charsets)
    assert out[0]["IPTC:CodedCharacterSet"] == "UTF8"
    assert out[1]["IPTC:CodedCharacterSet"] == ""
    assert out[1]["IPTC:ObjectName"] == "Noël", "Latin-1 can hold it, so legacy IPTC still gets it"


def test_legacy_iptc_gets_only_what_latin1_can_hold_and_xmp_gets_everything() -> None:
    row = approved("C:/a/old.jpg", title="Łódź Trip", keywords="Łódź;beach")
    _, out = build_applied_rows([row], xmp_enabled=True, iptc_charsets={"C:/a/old.jpg": "legacy"})
    assert out[0]["IPTC:ObjectName"] == ""
    assert out[0]["IPTC:Keywords"] == "beach"
    assert out[0]["XMP:Title"] == "Łódź Trip"
    assert out[0]["XMP:Subject"] == "Łódź;beach"


def test_a_date_only_row_declares_no_iptc_charset() -> None:
    row = approved("C:/a/b.jpg", title="", keywords="")
    _, out = build_applied_rows([row], xmp_enabled=True, iptc_charsets={"C:/a/b.jpg": "none"})
    assert out[0]["IPTC:CodedCharacterSet"] == ""


def test_heic_gets_no_iptc() -> None:
    """Verified: exiftool reports success but stores no IPTC in HEIC."""
    _, out = build_applied_rows([approved("C:/a/b.heic")], xmp_enabled=True)
    assert out[0]["IPTC:ObjectName"] == "" and out[0]["XMP:Title"] == "Trip"


def test_an_approved_file_exiftool_could_not_read_is_refused(tmp_path: Path) -> None:
    path = files_csv(tmp_path, [file_row(apply=1, date_flags="no_exif_date,exiftool_error")])
    approved_rows, problems = load_files(path, {})
    assert approved_rows == []
    assert "could not read" in problems[0]


def test_a_csv_saved_in_a_legacy_encoding_gets_an_actionable_error(tmp_path: Path) -> None:
    path = tmp_path / "folders.csv"
    path.write_bytes("folder_path\nCaf\xe9\n".encode("cp1252"))
    with pytest.raises(CsvEncodingError, match="CSV UTF-8"):
        load_folders(path)


def test_failed_files_are_counted_as_errors_not_updates() -> None:
    counts = parse_update_counts("    3 image files updated\n    1 files weren't updated due to errors\n")
    assert counts == {"updated": 3, "unchanged": 0, "errors": 1}


def test_fit_iptc_never_splits_a_multibyte_character() -> None:
    got = fit_iptc("é" * 40)
    assert len(got.encode("utf-8")) <= 64
    assert got == "é" * 32


def test_disk_space_is_measured_on_the_photos_own_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    seen: list[Path] = []

    def fake_usage(path: Path) -> Any:
        seen.append(Path(path))
        return type("Usage", (), {"free": 10**9})()

    monkeypatch.setattr("photofix.apply.shutil.disk_usage", fake_usage)
    ok, needed, _ = check_disk_space([{"SourceFile": str(f)}], None)
    assert ok and needed == 10
    assert seen == [tmp_path]


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def seeded_store(tmp_path: Path, files: list[Path]) -> ScanStore:
    store = ScanStore(tmp_path / "scan.db")
    store.upsert_walked(
        [
            WalkedFile(
                path=p, size=p.stat().st_size, mtime=p.stat().st_mtime, birthtime=None, inode=p.stat().st_ino
            )
            for p in files
        ]
    )
    return store


def snapshot(f: Path) -> dict[str, Any]:
    st = f.stat()
    return {"scan_size": st.st_size, "scan_mtime": st.st_mtime}


def carrying(
    date: str = "2019:07:04 12:00:00", title: str = "Trip", keywords: Sequence[str] = ("beach", "summer")
) -> dict[str, Any]:
    """What a .jpg looks like after apply wrote `approved()`'s defaults."""
    return {
        "EXIF:DateTimeOriginal": date,
        "EXIF:CreateDate": date,
        "XMP:DateCreated": date,
        "IPTC:ObjectName": fit_iptc(title),
        "XMP:Title": title,
        "IPTC:Keywords": list(keywords),
        "XMP:Subject": list(keywords),
    }


def changed_since_scan(f: Path) -> dict[str, Any]:
    """The row's snapshot, taken before the file is then modified."""
    before = snapshot(f)
    f.write_bytes(f.read_bytes() + b"!")
    return before


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
        rows = [approved(str(f), **changed_since_scan(f))]
        outcome = guard(rows, store, FakeExifTool({normalize_key(f): carrying()}))
    assert outcome.clean == []
    assert len(outcome.already_applied) == 1
    assert outcome.blocking == []


def test_a_file_carrying_only_part_of_the_intent_is_refused(tmp_path: Path) -> None:
    """IPTC written but XMP not is a half-done write, not a finished one."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), **changed_since_scan(f))]
        partial = {k: v for k, v in carrying().items() if not k.startswith("XMP:")}
        outcome = guard(rows, store, FakeExifTool({normalize_key(f): partial}))
    assert len(outcome.modified_elsewhere) == 1


def test_a_file_written_before_is_caught_by_inode_when_size_and_mtime_match(tmp_path: Path) -> None:
    """exiftool writes a new file; -P on a whole-second mtime can hide that from size and mtime."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    os.utime(f, (1_600_000_000, 1_600_000_000))
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), date="2020:01:01 12:00:00", **snapshot(f))]
        replacement = tmp_path / "new.jpg"
        replacement.write_bytes(b"y" * 10)
        os.utime(replacement, (1_600_000_000, 1_600_000_000))
        replacement.replace(f)
        tool = FakeExifTool({normalize_key(f): carrying(date="2019:07:04 12:00:00")})
        outcome = guard(rows, store, tool)
    assert len(outcome.modified_elsewhere) == 1


def test_a_new_inode_with_untouched_metadata_is_still_clean(tmp_path: Path) -> None:
    """Network volumes can renumber inodes; that alone must not refuse a file."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), **snapshot(f))]
        st = f.stat()
        replacement = tmp_path / "new.jpg"
        replacement.write_bytes(f.read_bytes())
        os.utime(replacement, ns=(st.st_atime_ns, st.st_mtime_ns))
        replacement.replace(f)
        outcome = guard(rows, store, FakeExifTool({normalize_key(f): {"File:MIMEType": "image/jpeg"}}))
    assert len(outcome.clean) == 1


def test_a_gif_carrying_the_intent_in_xmp_is_skipped_not_refused(tmp_path: Path) -> None:
    """GIF dates land in XMP:DateCreated, so that is where the guard must look."""
    f = tmp_path / "a.gif"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), **changed_since_scan(f))]
        tool = FakeExifTool(
            {
                normalize_key(f): {
                    "XMP:DateCreated": "2019:07:04 12:00:00",
                    "XMP:Title": "Trip",
                    "XMP:Subject": ["beach", "summer"],
                }
            }
        )
        outcome = guard(rows, store, tool)
    assert len(outcome.already_applied) == 1
    assert outcome.blocking == []


def test_a_long_title_cut_short_in_iptc_still_counts_as_applied(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), title=LONG_TITLE, keywords="", **changed_since_scan(f))]
        tool = FakeExifTool({normalize_key(f): carrying(title=LONG_TITLE, keywords=())})
        outcome = guard(rows, store, tool)
    assert len(outcome.already_applied) == 1
    assert outcome.blocking == []


def test_a_file_changed_into_something_else_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), **changed_since_scan(f))]
        tool = FakeExifTool({normalize_key(f): {"EXIF:DateTimeOriginal": "1999:01:01 00:00:00"}})
        outcome = guard(rows, store, tool)
    assert outcome.clean == []
    assert len(outcome.modified_elsewhere) == 1
    assert outcome.blocking


def test_a_finished_merge_run_counts_as_applied(tmp_path: Path) -> None:
    """A merge run writes the union of the scan-time keywords and the new ones."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        store.record_metadata(
            {normalize_key(f): {"IPTC:Keywords": ["preexisting", "Beach"], "XMP:Subject": ["preexisting"]}}
        )
        rows = [approved(str(f), date="", title="", **changed_since_scan(f))]
        merged = {
            "IPTC:Keywords": ["preexisting", "Beach", "summer"],
            "XMP:Subject": ["preexisting", "beach", "summer"],
        }
        outcome = guard(rows, store, FakeExifTool({normalize_key(f): merged}), merge_keywords=True)
    assert len(outcome.already_applied) == 1


def test_merge_keywords_writes_the_union_without_duplicates() -> None:
    meta = {"IPTC:Keywords": ["Beach", "Mom"], "XMP:Subject": ["beach", "Mom"]}
    _, rows = build_applied_rows(
        [approved("/a/b.jpg", keywords="Beach;Trip")],
        xmp_enabled=True,
        iptc_charsets={"/a/b.jpg": "utf8"},
        metas={"/a/b.jpg": meta},
        merge_keywords=True,
    )
    assert rows[0]["IPTC:Keywords"] == "Beach;Mom;Trip"
    assert rows[0]["XMP:Subject"] == "Beach;Mom;Trip"


def test_merge_keywords_writes_one_union_to_both_groups() -> None:
    meta = {"IPTC:Keywords": ["Old"]}
    _, rows = build_applied_rows(
        [approved("/a/b.jpg", keywords="Trip")],
        xmp_enabled=True,
        iptc_charsets={"/a/b.jpg": "utf8"},
        metas={"/a/b.jpg": meta},
        merge_keywords=True,
    )
    assert rows[0]["IPTC:Keywords"] == rows[0]["XMP:Subject"] == "Old;Trip"


def test_without_merge_existing_keywords_are_not_carried_over() -> None:
    _, rows = build_applied_rows(
        [approved("/a/b.jpg", keywords="Trip")],
        xmp_enabled=True,
        iptc_charsets={"/a/b.jpg": "utf8"},
        metas={"/a/b.jpg": {"IPTC:Keywords": ["Old"]}},
    )
    assert rows[0]["IPTC:Keywords"] == rows[0]["XMP:Subject"] == "Trip"


def test_typed_keywords_are_trimmed_and_deduplicated(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row("C:/a")]))
    row = file_row(override_keywords=" Beach; Family;; beach ; Mom;")
    approved_rows, _ = load_files(files_csv(tmp_path, [row]), folders)
    assert approved_rows[0].keywords == "Beach;Family;Mom"
    _, rows = build_applied_rows(approved_rows, xmp_enabled=True)
    assert rows[0]["XMP:Subject"] == "Beach;Family;Mom"


def test_values_with_nowhere_to_go_are_reported() -> None:
    dropped: list[str] = []
    build_applied_rows(
        [approved("/a/b.jpg", title="Tokyo 東京", keywords="Ελλάδα;Family")],
        xmp_enabled=False,
        iptc_charsets={"/a/b.jpg": "legacy"},
        dropped=dropped,
    )
    assert any("title 'Tokyo 東京'" in d for d in dropped)
    assert any("keywords 'Ελλάδα'" in d for d in dropped)
    assert not any("Family" in d for d in dropped)

    dropped.clear()
    build_applied_rows([approved("/a/b.gif")], xmp_enabled=False, dropped=dropped)
    assert len(dropped) == 3, "a GIF without XMP stores nothing"


def test_applied_csv_accumulates_across_runs(tmp_path: Path) -> None:
    """A resumed run must not drop the first run's files from what verify checks."""
    path = tmp_path / "applied.csv"
    columns = ["SourceFile", "XMP:Title"]
    record_applied(path, columns, [{"SourceFile": "/a/1.jpg", "XMP:Title": "One"}])
    record_applied(
        path,
        [*columns, "XMP:Subject"],
        [
            {"SourceFile": "/a/2.jpg", "XMP:Title": "Two", "XMP:Subject": "x"},
            {"SourceFile": "/a/1.jpg", "XMP:Title": "Uno"},
        ],
    )
    rows = list(read_review_csv(path))
    assert {r["SourceFile"]: r["XMP:Title"] for r in rows} == {"/a/1.jpg": "Uno", "/a/2.jpg": "Two"}


def test_older_csv_column_names_still_load(tmp_path: Path) -> None:
    folders_path = tmp_path / "folders.csv"
    old = [
        "folder_path",
        "file_count",
        "folder_name",
        "proposed_title",
        "proposed_keywords",
        "proposed_date",
        "skip",
    ]
    write_review_csv(folders_path, old, [["C:/a", 1, "a", "Old Title", "old", "", 0]])
    folders = load_folders(folders_path)
    assert folders[normalize_key("C:/a")].title == "Old Title"

    row = file_row()
    row["proposed_title"] = row.pop("override_title")
    row["proposed_title"] = "Typed"
    files_path = tmp_path / "files.csv"
    write_review_csv(files_path, list(row), [row])
    approved_rows, _ = load_files(files_path, folders)
    assert approved_rows[0].title == "Typed"


def test_a_deleted_file_is_a_conflict_not_a_crash(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), **snapshot(f))]
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


def test_a_scan_mtime_rounded_by_excel_still_passes(tmp_path: Path) -> None:
    """Excel keeps 15 significant digits; 1700000000.123456 has 16."""
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        snap = snapshot(f)
        rounded = float(f"{snap['scan_mtime']:.15g}") + 0.00005
        outcome = guard(
            [approved(str(f), scan_size=snap["scan_size"], scan_mtime=rounded)], store, FakeExifTool()
        )
    assert len(outcome.clean) == 1


def test_a_files_csv_from_an_older_scan_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with (
        seeded_store(tmp_path, [f]) as store,
        pytest.raises(ValidationError, match="re-run since this files.csv"),
    ):
        guard([approved(str(f), scan_size=999, scan_mtime=1.0)], store, FakeExifTool())


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
            approved(str(dirty), **changed_since_scan(dirty)),
        ]
        tool = FakeExifTool()
        guard(rows, store, tool)
    assert len(tool.calls) == 1
    assert [p.name for p in tool.calls[0]] == ["dirty.jpg"]


# ---------------------------------------------------------------------------
# scan.db, not the CSV, decides what a photo already has
# ---------------------------------------------------------------------------


def _store_with(tmp_path: Path, meta: dict[str, Any]) -> tuple[ScanStore, Path]:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    store = seeded_store(tmp_path, [f])
    store.record_metadata({normalize_key(f): {**meta, READ_VERSION_TAG: READ_VERSION}})
    return store, f


def _row_for(f: Path, **kw: Any) -> dict[str, Any]:
    return file_row(SourceFile=str(f), folder_path=str(f.parent), filename=f.name, **snapshot(f), **kw)


def test_deleting_the_context_columns_cannot_unlock_a_real_title(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row(str(tmp_path), "Hawaii", "hawaii")]))
    store, f = _store_with(tmp_path, {"XMP:Title": "Grandma's 80th", "IPTC:Keywords": ["Grandma"]})
    with store:
        row = _row_for(f, filename_title="Titled", existing_title="", existing_keywords="")
        approved_rows, problems = load_files(files_csv(tmp_path, [row]), folders, store=store)
    assert problems == []
    assert approved_rows[0].title == "" and approved_rows[0].keywords == ""


def test_a_blanked_error_flag_cannot_unlock_an_unread_file(tmp_path: Path) -> None:
    store, f = _store_with(tmp_path, {})
    with store:
        store.mark_unreadable([str(f)], "permission denied")
        _, problems = load_files(files_csv(tmp_path, [_row_for(f, date_flags="")]), {}, store=store)
    assert problems and "could not read" in problems[0]


def test_a_photo_with_no_title_in_scan_db_still_gets_one(tmp_path: Path) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row(str(tmp_path), "Hawaii", "hawaii")]))
    store, f = _store_with(tmp_path, {"XMP:Title": "OLYMPUS DIGITAL CAMERA"})
    with store:
        rows, _ = load_files(files_csv(tmp_path, [_row_for(f, existing_title="Real")]), folders, store=store)
    assert rows[0].title == "Hawaii" and rows[0].keywords == "hawaii"


def test_a_scan_db_read_with_fewer_tags_is_refused(tmp_path: Path) -> None:
    """It cannot know about keywords that only live in a tag it never asked for."""
    store, f = _store_with(tmp_path, {})
    with store:
        store.record_metadata({normalize_key(f): {READ_VERSION_TAG: "older"}})
        _, problems = load_files(files_csv(tmp_path, [_row_for(f)]), {}, store=store)
    assert problems and "Re-run the scan" in problems[0]


def test_a_rescan_re_reads_files_read_with_fewer_tags(tmp_path: Path) -> None:
    store, f = _store_with(tmp_path, {})
    with store:
        assert store.forget_stale_reads(READ_VERSION) == 0
        store.record_metadata({normalize_key(f): {READ_VERSION_TAG: "older"}})
        assert store.forget_stale_reads(READ_VERSION) == 1
        assert store.count(pending_only=True) == 1


@pytest.mark.parametrize("tag", ["XMP:TagsList", "XMP:HierarchicalSubject", "EXIF:XPKeywords"])
def test_keywords_in_any_known_tag_count_as_existing(tmp_path: Path, tag: str) -> None:
    folders = load_folders(folders_csv(tmp_path, [folder_row(str(tmp_path), "Hawaii", "hawaii")]))
    store, f = _store_with(tmp_path, {tag: ["Family"]})
    with store:
        rows, _ = load_files(files_csv(tmp_path, [_row_for(f)]), folders, store=store)
    assert rows[0].keywords == ""


def test_a_whole_second_of_spreadsheet_rounding_is_tolerated(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        st = f.stat()
        outcome = guard(
            [approved(str(f), scan_size=st.st_size, scan_mtime=float(int(st.st_mtime)))],
            store,
            FakeExifTool(),
        )
    assert len(outcome.clean) == 1


# ---------------------------------------------------------------------------
# Every date tag, and nothing stale left beside it
# ---------------------------------------------------------------------------


def test_date_tags_the_file_already_has_are_rewritten_too() -> None:
    meta = {
        "XMP:DateTimeOriginal": "2002:12:08 12:00:00",
        "XMP:CreateDate": "x",
        "IPTC:DateCreated": "2002:12:08",
    }
    _, rows = build_applied_rows([approved("/a/b.jpg")], xmp_enabled=True, metas={"/a/b.jpg": meta})
    row = rows[0]
    assert row["XMP:DateTimeOriginal"] == row["XMP:CreateDate"] == "2019:07:04 12:00:00"
    assert row["IPTC:DateCreated"] == "2019:07:04"


def test_date_tags_the_file_lacks_are_not_added() -> None:
    _, rows = build_applied_rows([approved("/a/b.jpg")], xmp_enabled=True, metas={"/a/b.jpg": {}})
    assert rows[0]["XMP:DateTimeOriginal"] == rows[0]["IPTC:DateCreated"] == ""


def test_a_new_date_deletes_the_old_offset_and_subseconds() -> None:
    meta = {"IPTC:TimeCreated": "12:00:00+09:00"}
    _, rows = build_applied_rows([approved("/a/b.jpg")], xmp_enabled=True, metas={"/a/b.jpg": meta})
    row = rows[0]
    for tag in (
        "EXIF:OffsetTimeOriginal",
        "EXIF:SubSecTimeOriginal",
        "EXIF:OffsetTimeDigitized",
        "IPTC:TimeCreated",
    ):
        assert row[tag] == DELETE_MARKER, tag


def test_a_text_only_row_deletes_nothing() -> None:
    _, rows = build_applied_rows([approved("/a/b.jpg", date="")], xmp_enabled=True)
    assert DELETE_MARKER not in rows[0].values()


def test_a_gif_rewrites_its_xmp_date_and_nothing_else() -> None:
    meta = {"XMP:DateTimeOriginal": "2002:12:08 12:00:00"}
    _, rows = build_applied_rows([approved("/a/b.gif")], xmp_enabled=True, metas={"/a/b.gif": meta})
    row = rows[0]
    assert row["XMP:DateTimeOriginal"] == "2019:07:04 12:00:00"
    assert row["EXIF:DateTimeOriginal"] == row["EXIF:OffsetTimeOriginal"] == ""


def test_legacy_iptc_that_cannot_take_a_title_has_its_old_one_removed() -> None:
    """Otherwise "Vieux Café" stays in IPTC beside "東京 Noël" in XMP."""
    meta = {"IPTC:ObjectName": "Vieux Café", "IPTC:Keywords": ["Crème"]}
    _, rows = build_applied_rows(
        [approved("/a/b.jpg", title="東京 Noël", keywords="東京")],
        xmp_enabled=True,
        iptc_charsets={"/a/b.jpg": "legacy"},
        metas={"/a/b.jpg": meta},
    )
    row = rows[0]
    assert row["IPTC:ObjectName"] == row["IPTC:Keywords"] == DELETE_MARKER
    assert row["XMP:Title"] == "東京 Noël" and row["XMP:Subject"] == "東京"


def test_without_xmp_an_unstorable_title_leaves_the_old_one_alone() -> None:
    meta = {"IPTC:ObjectName": "Vieux Café"}
    _, rows = build_applied_rows(
        [approved("/a/b.jpg", title="東京", keywords="")],
        xmp_enabled=False,
        iptc_charsets={"/a/b.jpg": "legacy"},
        metas={"/a/b.jpg": meta},
    )
    assert rows[0]["IPTC:ObjectName"] == ""


def test_a_resumed_row_with_deletions_is_recognized(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        store.record_metadata({normalize_key(f): {"EXIF:OffsetTimeOriginal": "+09:00"}})
        rows = [approved(str(f), **changed_since_scan(f))]
        outcome = guard(rows, store, FakeExifTool({normalize_key(f): carrying()}))
    assert len(outcome.already_applied) == 1


def test_a_no_op_row_is_not_reported_as_already_applied(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        rows = [approved(str(f), date="", title="", keywords="", **changed_since_scan(f))]
        outcome = guard(rows, store, FakeExifTool({normalize_key(f): {"XMP:Title": "Edited"}}))
    assert outcome.already_applied == [] and len(outcome.clean) == 1


# ---------------------------------------------------------------------------
# Files exiftool would fail on, or should not touch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_a_read_only_file_is_refused(tmp_path: Path) -> None:
    f = tmp_path / "locked.jpg"
    f.write_bytes(b"x" * 10)
    with seeded_store(tmp_path, [f]) as store:
        f.chmod(0o444)
        try:
            outcome = guard([approved(str(f), **snapshot(f))], store, FakeExifTool())
        finally:
            f.chmod(0o644)
    assert [c.classification for c in outcome.blocking] == ["unwritable"]
    assert "read-only" in outcome.blocking[0].reason
    assert outcome.blocking[0].actual_size == 10


def test_write_blocker_names_each_problem(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    assert write_blocker(f) == ""
    (tmp_path / "a.jpg_exiftool_tmp").write_bytes(b"")
    assert "leftover" in write_blocker(f)
    assert "too long" in write_blocker(tmp_path / ("x" * 240 + ".jpg"))


# ---------------------------------------------------------------------------
# Verify's baseline survives a rescan
# ---------------------------------------------------------------------------


def test_baselines_outlive_a_rescan(tmp_path: Path) -> None:
    store, f = _store_with(tmp_path, {"XMP:Title": "before"})
    with store:
        store.save_baselines(store.get_many([str(f)]).values())
        store.record_metadata({normalize_key(f): {"XMP:Title": "after"}})
        assert store.baselines([str(f)])[normalize_key(f)]["XMP:Title"] == "before"


def test_baselines_fall_back_to_the_scan_for_files_apply_never_wrote(tmp_path: Path) -> None:
    store, f = _store_with(tmp_path, {"XMP:Title": "scan"})
    with store:
        assert store.baselines([str(f)])[normalize_key(f)]["XMP:Title"] == "scan"


def test_applied_csv_is_replaced_whole(tmp_path: Path) -> None:
    path = tmp_path / "applied.csv"
    record_applied(path, ["SourceFile", "XMP:Title"], [{"SourceFile": "/a.jpg", "XMP:Title": "A"}])
    assert not (tmp_path / "applied.csv.partial").exists()
    assert [r["XMP:Title"] for r in read_review_csv(path)] == ["A"]
