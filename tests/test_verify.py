"""Verify's three buckets, and the normalization that keeps them honest.

The normalization tests exist because of a specific operational trap: without
them, verify reports near-total failure on a run that succeeded completely, and
the natural response to "48,000 files not applied" is to try to undo a correct
change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from photofix.exiftool import DELETE_MARKER
from photofix.verify import compare, find_strays, load_intent, normalize_value

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written,read_back",
    [
        ("2019:07:04 12:00:00", "2019:07:04 12:00:00"),
        ("2019:07:04 12:00:00", "2019:07:04 12:00:00.00"),
        ("2019:07:04 12:00:00", "2019:07:04 12:00:00+01:00"),
        ("2019:07:04 12:00:00", "2019:07:04 12:00:00-07:00"),
    ],
)
def test_a_date_survives_the_round_trip_in_any_of_its_spellings(written: str, read_back: str) -> None:
    tag = "EXIF:DateTimeOriginal"
    assert normalize_value(tag, written) == normalize_value(tag, read_back)


def test_keywords_compare_regardless_of_order_and_case() -> None:
    """exiftool returns a list in storage order; the CSV wrote a ';' string."""
    tag = "IPTC:Keywords"
    assert normalize_value(tag, "beach;summer") == normalize_value(tag, ["Summer", "Beach"])


def test_keyword_whitespace_is_ignored() -> None:
    tag = "IPTC:Keywords"
    assert normalize_value(tag, "beach; summer") == normalize_value(tag, ["beach", "summer"])


def test_empty_keyword_entries_are_dropped() -> None:
    tag = "IPTC:Keywords"
    assert normalize_value(tag, "beach;;summer") == normalize_value(tag, ["beach", "summer"])


def test_title_comparison_is_case_sensitive() -> None:
    """A title is prose. "Hawaii" and "HAWAII" are genuinely different values."""
    tag = "IPTC:ObjectName"
    assert normalize_value(tag, "Hawaii") != normalize_value(tag, "HAWAII")


def test_none_normalizes_to_empty() -> None:
    assert normalize_value("IPTC:ObjectName", None) == ""


def test_merged_keywords_verify_against_the_union_applied_csv_holds() -> None:
    intent = {"a.jpg": {"IPTC:Keywords": "family;Christmas"}}
    actual = {"a.jpg": {"IPTC:Keywords": ["family", "Christmas"]}}
    report = compare(intent, actual, {"a.jpg": {"IPTC:Keywords": ["family"]}})
    assert report.not_applied == [] and len(report.verified) == 1


def test_an_override_that_left_old_keywords_behind_fails() -> None:
    """Keywords that were there before are not excused: a replace must replace."""
    intent = {"a.jpg": {"IPTC:Keywords": "Christmas"}}
    actual = {"a.jpg": {"IPTC:Keywords": ["family", "Christmas"]}}
    report = compare(intent, actual, {"a.jpg": {"IPTC:Keywords": ["family"]}})
    assert len(report.not_applied) == 1


def test_a_deleted_tag_verifies_as_absent() -> None:
    intent = {"a.jpg": {"EXIF:OffsetTimeOriginal": DELETE_MARKER}}
    assert len(compare(intent, {"a.jpg": {}}).verified) == 1
    assert len(compare(intent, {"a.jpg": {"EXIF:OffsetTimeOriginal": "+09:00"}}).not_applied) == 1


# ---------------------------------------------------------------------------
# Buckets
# ---------------------------------------------------------------------------

INTENT = {"a.jpg": {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00", "IPTC:Keywords": "beach;summer"}}


def test_everything_present_verifies() -> None:
    actual = {"a.jpg": {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00", "IPTC:Keywords": ["summer", "beach"]}}
    report = compare(INTENT, actual)
    assert len(report.verified) == 2
    assert report.failures == 0


def test_a_missing_value_lands_in_not_applied() -> None:
    actual = {"a.jpg": {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00"}}
    report = compare(INTENT, actual)
    assert len(report.verified) == 1
    assert len(report.not_applied) == 1
    assert report.not_applied[0].tag == "IPTC:Keywords"


def test_an_unreadable_file_lands_in_not_applied() -> None:
    report = compare(INTENT, {})
    assert len(report.not_applied) == 1
    assert "could not be re-read" in report.not_applied[0].detail


def test_a_tag_that_changed_without_being_asked_for_is_unexpected() -> None:
    """The bucket that catches the tool doing something it was never told to."""
    baseline = {"a.jpg": {"IPTC:ObjectName": "Original Title"}}
    actual = {
        "a.jpg": {
            "EXIF:DateTimeOriginal": "2019:07:04 12:00:00",
            "IPTC:Keywords": ["beach", "summer"],
            "IPTC:ObjectName": "Something Else",
        }
    }
    report = compare(INTENT, actual, baseline)
    assert len(report.unexpected) == 1
    assert report.unexpected[0].tag == "IPTC:ObjectName"
    assert report.failures == 1


def test_an_untouched_tag_is_not_reported() -> None:
    baseline = {"a.jpg": {"IPTC:ObjectName": "Original Title"}}
    actual = {
        "a.jpg": {
            "EXIF:DateTimeOriginal": "2019:07:04 12:00:00",
            "IPTC:Keywords": ["beach", "summer"],
            "IPTC:ObjectName": "Original Title",
        }
    }
    report = compare(INTENT, actual, baseline)
    assert report.unexpected == []
    assert report.failures == 0


def test_without_a_baseline_unexpected_detection_is_simply_off() -> None:
    actual = {
        "a.jpg": {
            "EXIF:DateTimeOriginal": "2019:07:04 12:00:00",
            "IPTC:Keywords": ["beach", "summer"],
            "IPTC:ObjectName": "Appeared From Nowhere",
        }
    }
    report = compare(INTENT, actual, None)
    assert report.unexpected == []
    assert report.failures == 0


def test_a_requested_tag_is_never_double_counted_as_unexpected() -> None:
    baseline = {"a.jpg": {"EXIF:DateTimeOriginal": "1999:01:01 00:00:00"}}
    actual = {"a.jpg": {"EXIF:DateTimeOriginal": "2019:07:04 12:00:00", "IPTC:Keywords": ["beach", "summer"]}}
    report = compare(INTENT, actual, baseline)
    assert report.unexpected == [], "we asked for this change; it is the success case"
    assert len(report.verified) == 2


# ---------------------------------------------------------------------------
# applied.csv round trip and _original sweep
# ---------------------------------------------------------------------------


def test_load_intent_ignores_blank_cells(tmp_path: Path) -> None:
    """A blank cell was never an instruction, so it is not something to verify."""
    path = tmp_path / "applied.csv"
    path.write_text(
        "SourceFile,EXIF:DateTimeOriginal,IPTC:Keywords\nC:/a/b.jpg,2019:07:04 12:00:00,\n",
        encoding="utf-8",
    )
    intent = load_intent(path)
    assert list(intent.values()) == [{"EXIF:DateTimeOriginal": "2019:07:04 12:00:00"}]


def test_find_strays_reports_original_backups(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.jpg").write_bytes(b"x")
    (tmp_path / "sub" / "a.jpg_original").write_bytes(b"x")
    strays = find_strays(tmp_path)
    assert [Path(s).name for s in strays] == ["a.jpg_original"]


def test_find_strays_does_not_delete_anything(tmp_path: Path) -> None:
    """The backups are the only undo path; removing them is a human's call."""
    backup = tmp_path / "a.jpg_original"
    backup.write_bytes(b"x")
    find_strays(tmp_path)
    assert backup.exists()
