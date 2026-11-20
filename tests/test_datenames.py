"""The centerpiece test. A wrong answer in datenames writes a wrong capture time.

Both tables matter equally. The negative table is what stops camera sequence
numbers being read as dates; the positive table is what stops an over-eager
guard from rejecting real ones. This file has already caught one bug of each
kind during development, which is why the named regressions below are kept
separate from the general tables even where they look redundant.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from photofix.datenames import (
    is_camera_sequence,
    parse_filename_date,
    parse_folder_date,
    strip_extension,
)

# Fixed "now" so the suite is deterministic and the sanity range never drifts.
NOW = datetime(2026, 9, 21, 12, 0, 0)


# ---------------------------------------------------------------------------
# Positives: (filename, expected datetime, expected match substring, rule, has_time)
# ---------------------------------------------------------------------------
POSITIVES = [
    # Separated Y-M-D, with and without a time.
    ("2019-07-04.jpg", datetime(2019, 7, 4, 12, 0, 0), "2019-07-04", "ymd_sep", False),
    ("2019_07_04.jpg", datetime(2019, 7, 4, 12, 0, 0), "2019_07_04", "ymd_sep", False),
    (
        "2019-07-04 12-30-45.jpg",
        datetime(2019, 7, 4, 12, 30, 45),
        "2019-07-04 12-30-45",
        "ymd_sep",
        True,
    ),
    (
        "2019-07-04_15-30-45.jpg",
        datetime(2019, 7, 4, 15, 30, 45),
        "2019-07-04_15-30-45",
        "ymd_sep",
        True,
    ),
    ("vacation 2019-07-04.png", datetime(2019, 7, 4, 12, 0, 0), "2019-07-04", "ymd_sep", False),
    # Compact YYYYMMDD, bare and prefixed.
    ("20190704.jpg", datetime(2019, 7, 4, 12, 0, 0), "20190704", "ymd_compact", False),
    ("IMG_20190704.jpg", datetime(2019, 7, 4, 12, 0, 0), "20190704", "ymd_compact", False),
    (
        "IMG_20190704_123045.jpg",
        datetime(2019, 7, 4, 12, 30, 45),
        "20190704_123045",
        "ymd_compact",
        True,
    ),
    (
        "VID_20190704_123045.mp4",
        datetime(2019, 7, 4, 12, 30, 45),
        "20190704_123045",
        "ymd_compact",
        True,
    ),
    (
        "MVIMG_20190704_123045.jpg",
        datetime(2019, 7, 4, 12, 30, 45),
        "20190704_123045",
        "ymd_compact",
        True,
    ),
    # WhatsApp.
    ("IMG-20190704-WA0001.jpg", datetime(2019, 7, 4, 12, 0, 0), "IMG-20190704-WA0001", "whatsapp", False),
    ("VID-20200101-WA0042.mp4", datetime(2020, 1, 1, 12, 0, 0), "VID-20200101-WA0042", "whatsapp", False),
    # Pixel — must beat ymd_compact, which would otherwise take a prefix.
    (
        "PXL_20220101_123456789.jpg",
        datetime(2022, 1, 1, 12, 34, 56),
        "PXL_20220101_123456789",
        "pixel",
        True,
    ),
    (
        "PXL_20220101_123456789.MP.jpg",
        datetime(2022, 1, 1, 12, 34, 56),
        "PXL_20220101_123456789",
        "pixel",
        True,
    ),
    # Android screenshots.
    (
        "Screenshot_20190704-123045.png",
        datetime(2019, 7, 4, 12, 30, 45),
        "Screenshot_20190704-123045",
        "screenshot_android",
        True,
    ),
    # Epochs, token-fenced.
    ("1234567890.jpg", datetime.fromtimestamp(1234567890), "1234567890", "epoch_s", True),
    ("DSC_1234567890.jpg", datetime.fromtimestamp(1234567890), "1234567890", "epoch_s", True),
    (
        "photo_1500000000000.jpg",
        datetime.fromtimestamp(1500000000.0),
        "1500000000000",
        "epoch_ms",
        True,
    ),
]


@pytest.mark.parametrize("filename,expected,match,rule,has_time", POSITIVES)
def test_positive_filenames(filename: str, expected: datetime, match: str, rule: str, has_time: bool) -> None:
    got = parse_filename_date(filename, now=NOW)
    assert got is not None, f"{filename} should parse"
    assert got.dt == expected
    assert got.match == match
    assert got.rule == rule
    assert got.has_time is has_time


# ---------------------------------------------------------------------------
# The six mandated negatives, plus the extras named in the plan.
# ---------------------------------------------------------------------------
MANDATED_NEGATIVES = [
    "DSC_0704.jpg",
    "DSCN1999.jpg",
    "100_4567.jpg",
    "IMG_2019.jpg",
    "P1010101.jpg",
    "SAM_2020.jpg",
]

EXTRA_NEGATIVES = [
    "IMG_1234.jpg",
    "_MG_2020.CR2",
    "DSC_0704 (1).jpg",
    "1234201907045678.jpg",  # 8-digit date buried in a longer digit run
    "20191340.jpg",  # month 13, day 40 — calendar-invalid
    "99999999999.jpg",  # 11 digits: neither epoch length
    "DSCF1234.jpg",
    "PICT0001.jpg",
    "IMGP5555.jpg",
    "20190230.jpg",  # Feb 30 — calendar-invalid
    "2519-07-04.jpg",  # beyond the sanity range
    "1899-12-31.jpg",  # before the sanity range
]


@pytest.mark.parametrize("filename", MANDATED_NEGATIVES)
def test_mandated_negatives_never_match(filename: str) -> None:
    assert parse_filename_date(filename, now=NOW) is None, f"{filename} must not parse"


@pytest.mark.parametrize("filename", EXTRA_NEGATIVES)
def test_extra_negatives_never_match(filename: str) -> None:
    assert parse_filename_date(filename, now=NOW) is None, f"{filename} must not parse"


# ---------------------------------------------------------------------------
# Named regressions. Each guards a mistake actually made during design; none of
# them should be dropped as redundant with the tables above.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("20190704.jpg", datetime(2019, 7, 4, 12, 0, 0)),
        ("19990101.jpg", datetime(1999, 1, 1, 12, 0, 0)),
        ("1234567890.jpg", datetime.fromtimestamp(1234567890)),
    ],
)
def test_regression_bare_numeric_names_still_parse(filename: str, expected: datetime) -> None:
    """The positives an unsplit camera-sequence alternation silently killed.

    With guard 4 written as `^(DSC|...|\\d{3})[-_]?\\d{1,7}$`, the `\\d{3}` branch
    takes "201" out of "20190704" and `\\d{1,7}` takes "90704", so the whole stem
    matches and the parser returns None for a mandated positive. Splitting the
    numeric branch to require a separator is what fixes it.
    """
    got = parse_filename_date(filename, now=NOW)
    assert got is not None, f"{filename} must still parse after the guard-4 split"
    assert got.dt == expected


def test_regression_guard4_split_did_not_leak() -> None:
    """The split must not have traded a false negative for a false positive."""
    assert parse_filename_date("100_4567.jpg", now=NOW) is None
    assert is_camera_sequence("100_4567")
    # And the numeric branch still must not swallow a real date.
    got = parse_filename_date("100_20190704.jpg", now=NOW)
    assert got is not None and got.dt == datetime(2019, 7, 4, 12, 0, 0)


def test_regression_epoch_fences_on_token_not_digit() -> None:
    """`IMG1234567890` is not a whole token; `DSC_1234567890` is."""
    assert parse_filename_date("IMG1234567890.jpg", now=NOW) is None
    got = parse_filename_date("DSC_1234567890.jpg", now=NOW)
    assert got is not None
    assert got.rule == "epoch_s"
    assert got.match == "1234567890"


@pytest.mark.parametrize(
    "filename",
    [
        "Screen Shot 2019-07-04 at 1.23.45 PM.png",  # macOS through Mojave
        "Screenshot 2023-04-05 at 1.23.45 PM.png",  # Catalina onward
        "Screenshot 2023-04-05 at 1.23.45\u202fPM.png",  # Ventura onward: U+202F
    ],
)
def test_regression_both_screenshot_spellings(filename: str) -> None:
    got = parse_filename_date(filename, now=NOW)
    assert got is not None, f"{filename!r} must parse"
    assert got.rule == "screenshot_mac"
    assert got.dt.hour == 13 and got.dt.minute == 23 and got.dt.second == 45


# ---------------------------------------------------------------------------
# Meridiem conversion, which has two off-by-twelve traps.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,hour",
    [
        ("Screenshot 2023-04-05 at 12.00.00 AM.png", 0),
        ("Screenshot 2023-04-05 at 12.00.00 PM.png", 12),
        ("Screenshot 2023-04-05 at 1.00.00 AM.png", 1),
        ("Screenshot 2023-04-05 at 11.00.00 PM.png", 23),
    ],
)
def test_meridiem_conversion(filename: str, hour: int) -> None:
    got = parse_filename_date(filename, now=NOW)
    assert got is not None
    assert got.dt.hour == hour


# ---------------------------------------------------------------------------
# Bare years: folders only.
# ---------------------------------------------------------------------------


def test_bare_year_matches_in_folders_but_never_in_filenames() -> None:
    assert parse_filename_date("2019.jpg", now=NOW) is None
    folder = parse_folder_date("2019", now=NOW)
    assert folder is not None
    assert folder.rule == "bare_year"
    assert folder.dt == datetime(2019, 1, 1, 12, 0, 0)
    assert folder.has_time is False


def test_folder_prefers_a_full_date_over_a_bare_year() -> None:
    got = parse_folder_date("2019-07-04 Family Reunion", now=NOW)
    assert got is not None
    assert got.rule == "ymd_sep"
    assert got.dt == datetime(2019, 7, 4, 12, 0, 0)


def test_folder_with_no_date_returns_none() -> None:
    assert parse_folder_date("Family Reunion", now=NOW) is None


# ---------------------------------------------------------------------------
# The noon rule, which exists specifically to survive timezone interpretation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["2019-07-04.jpg", "20190704.jpg", "IMG_20190704.jpg"])
def test_date_only_candidates_land_on_noon_not_midnight(name: str) -> None:
    got = parse_filename_date(name, now=NOW)
    assert got is not None
    assert (got.dt.hour, got.dt.minute, got.dt.second) == (12, 0, 0)
    assert got.has_time is False


def test_future_dates_are_rejected() -> None:
    assert parse_filename_date("2030-01-01.jpg", now=NOW) is None


def test_sanity_range_is_relative_to_now() -> None:
    earlier = datetime(2019, 1, 1)
    assert parse_filename_date("2020-05-05.jpg", now=earlier) is None
    assert parse_filename_date("2018-05-05.jpg", now=earlier) is not None


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,stem",
    [
        ("IMG_2019.jpg", "IMG_2019"),
        ("photo.tar.gz", "photo.tar"),
        ("noextension", "noextension"),
        ("PXL_20220101_123456789.MP.jpg", "PXL_20220101_123456789.MP"),
    ],
)
def test_strip_extension(filename: str, stem: str) -> None:
    assert strip_extension(filename) == stem


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("DSC_0704", True),
        ("DSCN1999", True),
        ("DSCF1234", True),
        ("P1010101", True),
        ("SAM_2020", True),
        ("100_4567", True),
        ("IMG_2019", False),  # caught by the absence of a bare-year rule instead
        ("DSC_0704 (1)", False),  # the ` (1)` defeats the `$` anchor
        ("20190704", False),  # numeric branch needs a separator
        ("1234567890", False),
    ],
)
def test_camera_sequence_guard(stem: str, expected: bool) -> None:
    assert is_camera_sequence(stem) is expected


def test_time_component_out_of_range_degrades_to_noon() -> None:
    """A bad time drops to noon rather than discarding the date evidence."""
    got = parse_filename_date("20190704_996060.jpg", now=NOW)
    assert got is not None
    assert got.dt == datetime(2019, 7, 4, 12, 0, 0)
    assert got.has_time is False
