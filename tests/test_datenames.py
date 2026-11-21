"""The centerpiece test. A wrong answer in datenames writes a wrong capture time.

Both tables matter equally. The negative table is what stops camera sequence
numbers being read as dates; the positive table is what stops an over-eager
guard from rejecting real ones. This file has already caught one bug of each
kind during development, which is why the named regressions below are kept
separate from the general tables even where they look redundant.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from photofix.datenames import (
    is_camera_sequence,
    parse_filename_date,
    parse_folder_date,
    parse_folder_partial,
    parse_written_date,
    strip_extension,
    year_span,
)
from photofix.folders import classify_folder

# Fixed "now" so the suite is deterministic and the sanity range never drifts.
NOW = datetime(2026, 9, 21, 12, 0, 0)


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Partial dates in folder names: (name, year to resolve with, expected day, rule)
# ---------------------------------------------------------------------------
PARTIALS = [
    ("12.21 Barney on Christmas Eve", 2021, (12, 21), "month_day"),
    ("12-25 Morning", 2021, (12, 25), "month_day"),
    ("2021 12.21 Barney", 2021, (12, 21), "month_day"),  # after a leading year
    ("21.12 Party", 2021, (12, 21), "month_day"),  # month-first impossible, so day-first
    ("Dec 21 Barney", 2021, (12, 21), "month_name_day"),
    ("December 25th", 2021, (12, 25), "month_name_day"),
    ("Christmas Eve", 2021, (12, 24), "christmas_eve"),
    ("Christmas Day", 2021, (12, 25), "christmas"),
    ("Xmas", 2021, (12, 25), "christmas"),
    ("New Year's Eve", 2021, (12, 31), "new_years_eve"),
    ("New Years Day", 2021, (1, 1), "new_years_day"),
    ("Halloween", 2021, (10, 31), "halloween"),
    ("Valentine’s Day", 2021, (2, 14), "valentines_day"),
    ("4th of July BBQ", 2021, (7, 4), "month_name_day"),  # day-first month name
    ("Independence Day BBQ", 2021, (7, 4), "independence_day"),
    ("St. Patrick's Day", 2021, (3, 17), "st_patricks_day"),
    ("Thanksgiving", 2021, (11, 25), "thanksgiving"),
    ("Thanksgiving", 2022, (11, 24), "thanksgiving"),
    ("Easter", 2021, (4, 4), "easter"),
    ("Easter", 2024, (3, 31), "easter"),
    ("Mother's Day", 2021, (5, 9), "mothers_day"),
    ("Fathers Day", 2021, (6, 20), "fathers_day"),
    ("Memorial Day", 2021, (5, 31), "memorial_day"),
    ("Labor Day", 2021, (9, 6), "labor_day"),
]


@pytest.mark.parametrize("name,year,day,rule", PARTIALS)
def test_folder_partials(name: str, year: int, day: tuple[int, int], rule: str) -> None:
    got = parse_folder_partial(name, now=NOW)
    assert got is not None and got.rule == rule
    assert got.on(year, now=NOW) == datetime(year, *day, 12, 0, 0)


def test_a_numeric_month_day_can_carry_its_own_year() -> None:
    got = parse_folder_partial("12-25-2021 Christmas Morning", now=NOW)
    assert got is not None and got.year == 2021
    got = parse_folder_partial("12.21.21 Barney", now=NOW)
    assert got is not None and got.year == 2021


@pytest.mark.parametrize(
    "name,expected",
    [
        ("2019-07 Hawaii", datetime(2019, 7, 1, 12)),
        ("Hawaii 07-2019", datetime(2019, 7, 1, 12)),
        ("March 2019", datetime(2019, 3, 1, 12)),
        ("2019 March Madness", datetime(2019, 3, 1, 12)),
        ("2019-07-04 Fireworks", datetime(2019, 7, 4, 12)),  # a full date still wins
    ],
)
def test_month_year_folders_date_to_the_first_of_the_month(name: str, expected: datetime) -> None:
    got = parse_folder_date(name, now=NOW)
    assert got is not None and got.dt == expected


def test_day_first_month_names_are_dates() -> None:
    got = parse_folder_partial("21 Dec Party", now=NOW)
    assert got is not None and got.on(2021, now=NOW) == datetime(2021, 12, 21, 12)


@pytest.mark.parametrize("name", ["2.5 Years Old", "18-24 Months", "3.5 yrs"])
def test_ages_are_not_month_days(name: str) -> None:
    assert parse_folder_partial(name, now=NOW) is None


def test_a_mid_name_numeric_date_needs_a_year() -> None:
    got = parse_folder_partial("Barney 12.25.2021", now=NOW)
    assert got is not None and got.year == 2021
    got = parse_folder_partial("Christmas 12-25-98", now=NOW)
    assert got is not None and got.year == 1998 and got.rule == "month_day"
    got = parse_folder_partial("Jake 1st Bday 6-15-19", now=NOW)
    assert got is not None and got.on(got.year or 0, now=NOW) == datetime(2019, 6, 15, 12)
    assert parse_folder_partial("Barney 12.25", now=NOW) is None


@pytest.mark.parametrize(
    "name",
    [
        "1/2 Marathon 2019",
        "3.5 Mile Hike 2019",
        "6-8 Grade 2019",
        "5-4 Win 2019",
        "24-7 2019",
        "10-12 Kids 2019",
    ],
)
def test_fractions_distances_and_ranges_are_not_month_days(name: str) -> None:
    assert parse_folder_partial(name, now=NOW) is None


@pytest.mark.parametrize(
    "name,span",
    [
        ("Grandpa (1920-1995)", (1920, 1995)),
        ("Grandma 1925-2019 Funeral", (1925, 2019)),
        ("1985 Reunion 2015", (1985, 2015)),
        ("2019-2020 School Year", (2019, 2020)),
        ("Kindergarten 2019-20", (2019, 2020)),
    ],
)
def test_names_spanning_years_give_no_date(name: str, span: tuple[int, int]) -> None:
    assert year_span(name) == span
    assert parse_folder_date(name, now=NOW) is None


@pytest.mark.parametrize("name", ["2019-07 Hawaii", "2019-07-04", "Christmas 2019", "2010-12"])
def test_one_year_is_not_a_span(name: str) -> None:
    assert year_span(name) is None


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Christmas '98", datetime(1998, 1, 1, 12)),
        ("2015-3-14 Zoo", datetime(2015, 3, 14, 12)),
        ("Jan-Mar 2020", datetime(2020, 1, 1, 12)),  # a quarter, so only the year
        ("Aunt May 2015", datetime(2015, 1, 1, 12)),  # a person, so only the year
    ],
)
def test_more_folder_date_forms(name: str, expected: datetime) -> None:
    got = parse_folder_date(name, now=NOW)
    assert got is not None and got.dt == expected


@pytest.mark.parametrize("name", ["1990s", "Late 1960s", "The '80s"])
def test_decades_are_not_years(name: str) -> None:
    assert parse_folder_date(name, now=NOW) is None


def test_a_name_after_a_month_is_not_a_day() -> None:
    assert parse_folder_partial("Aunt May 80th Birthday", now=NOW) is None
    assert parse_folder_partial("Aunt May 5th", now=NOW) is None


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("WhatsApp Image 2019-07-04 at 15.05.12.jpeg", datetime(2019, 7, 4, 15, 5, 12)),
        ("signal-2019-07-04-150512.jpg", datetime(2019, 7, 4, 15, 5, 12)),
    ],
)
def test_times_after_a_date_are_kept(filename: str, expected: datetime) -> None:
    got = parse_filename_date(filename, now=NOW)
    assert got is not None and got.dt == expected


def test_facebook_download_ids_are_not_dates() -> None:
    assert parse_filename_date("20011223_10153412345678901_1234567890_n.jpg", now=NOW) is None


@pytest.mark.parametrize(
    "name",
    [
        "2021-12-25 Trip",
        "Hawaii Trip",
        "Route 66",
        "Season 13.14",
        "Game 3-2 Win",  # a score, not March 2nd
        "Kids 5-7 Soccer",  # an age range
        "App v1.2.10 Screens",  # a version, not January 2nd 2010
        "Easter Island",
        "Christmas Island",
        "St Patrick's Cathedral",
        "Valentine Family Reunion",  # a surname
    ],
)
def test_folder_partial_negatives(name: str) -> None:
    assert parse_folder_partial(name, now=NOW) is None


def test_a_partial_that_does_not_exist_in_that_year_resolves_to_nothing() -> None:
    got = parse_folder_partial("02.29 Leap Day", now=NOW)
    assert got is not None
    assert got.on(2021, now=NOW) is None
    assert got.on(2020, now=NOW) == datetime(2020, 2, 29, 12, 0, 0)


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
    # Pixel — must beat ymd_compact, which would otherwise take a prefix. The
    # name is UTC, so the expected value is that instant in local time.
    (
        "PXL_20220101_123456789.jpg",
        _utc(datetime(2022, 1, 1, 12, 34, 56)),
        "PXL_20220101_123456789",
        "pixel",
        True,
    ),
    (
        "PXL_20220101_123456789.MP.jpg",
        _utc(datetime(2022, 1, 1, 12, 34, 56)),
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
    ("FB_IMG_1563012345678.jpg", datetime.fromtimestamp(1563012345.678), "1563012345678", "epoch_ms", True),
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
    """`IMG1234567890123` is not a whole token; `photo_1234567890123` is."""
    assert parse_filename_date("IMG1234567890123.jpg", now=NOW) is None
    got = parse_filename_date("photo_1234567890123.jpg", now=NOW)
    assert got is not None
    assert got.rule == "epoch_ms"


@pytest.mark.parametrize(
    "filename",
    ["DSC_1234567890.jpg", "Snapchat-1187654321.jpg", "order 1012345678.jpg", "IMG1234567890.jpg"],
)
def test_ten_digit_numbers_inside_a_name_are_ids_not_epochs(filename: str) -> None:
    assert parse_filename_date(filename, now=NOW) is None


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


# ---------------------------------------------------------------------------
# Folder names people actually type
# ---------------------------------------------------------------------------
TYPED_FOLDERS = [
    ("Summer 2008", (2008, 7, 15), "season"),
    ("Summer '08", (2008, 7, 15), "season"),
    ("Spring 2010", (2010, 4, 15), "season"),
    ("Fall 2010", (2010, 10, 15), "season"),
    ("Autumn 2010", (2010, 10, 15), "season"),
    ("Winter 2008", (2008, 1, 15), "season"),
    ("NYE 2009", (2009, 12, 31), "new_years_eve"),
    ("Xmas 99", (1999, 12, 25), "christmas"),
    ("Christmas 04", (2004, 12, 25), "christmas"),
    ("Halloween 99", (1999, 10, 31), "halloween"),
    ("04.07.2003", (2003, 4, 7), "month_day_ambiguous"),
    ("25.12.2003", (2003, 12, 25), "month_day"),
    ("Christmas Dec 2010", (2010, 12, 25), "christmas"),
    ("2021-12-24 to 2021-12-26 Christmas", (2021, 12, 24), "ymd_sep"),
    ("1970", (1970, 1, 2), "bare_year"),  # Jan 1 of these years is a clock reset
    ("1980", (1980, 1, 2), "bare_year"),
    ("2010-11", (2010, 11, 1), "year_month"),  # no sport or school word: a month
]


@pytest.mark.parametrize("name,ymd,rule", TYPED_FOLDERS)
def test_typed_folder_dates(name: str, ymd: tuple[int, int, int], rule: str) -> None:
    got = classify_folder(name, now=NOW)
    assert got.date is not None, name
    assert (got.date.year, got.date.month, got.date.day) == ymd
    assert got.date_rule == rule


@pytest.mark.parametrize(
    "name",
    [
        "Hockey 2010-11",
        "School Year 2011-12",
        "2008-09 Basketball",
        "takeout-20200101T120000Z-001",
        "Backup Jan 2012",
    ],
)
def test_seasons_spanning_two_years_and_export_dates_give_nothing(name: str) -> None:
    assert classify_folder(name, now=NOW).date is None


def test_a_dotted_date_either_way_round_is_flagged_ambiguous() -> None:
    got = parse_folder_partial("04.07 Party", now=NOW)
    assert got is not None and got.rule == "month_day_ambiguous"
    same = parse_folder_partial("07.07 Party", now=NOW)
    assert same is not None and same.rule == "month_day"


# ---------------------------------------------------------------------------
# Device names, and dates people write into filenames
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename,expected",
    [
        ("WhatsApp Image 2023-07-04 at 3.05.12 PM.jpeg", datetime(2023, 7, 4, 15, 5, 12)),
        ("WhatsApp Image 2023-07-04 at 12.05.12 AM.jpeg", datetime(2023, 7, 4, 0, 5, 12)),
        ("Photo 2019-07-04 15 30 12.jpg", datetime(2019, 7, 4, 15, 30, 12)),
    ],
)
def test_times_with_meridiems_and_spaces(filename: str, expected: datetime) -> None:
    got = parse_filename_date(filename, now=NOW)
    assert got is not None and got.dt == expected


@pytest.mark.parametrize("filename", ["1563012345.jpg", "1563012345 (2).jpg"])
def test_a_ten_digit_name_alone_is_an_upload_time(filename: str) -> None:
    got = parse_filename_date(filename, now=NOW)
    assert got is not None and got.rule == "epoch_s"


@pytest.mark.parametrize(
    "filename,descriptive,ymd,rule",
    [
        ("Mom and Dad 12-25-1975.jpg", True, (1975, 12, 25), "month_day"),
        ("Photo on 7-4-19.jpg", False, (2019, 7, 4), "month_day"),
        ("CamScanner 07-04-2019 15.30.jpg", False, (2019, 7, 4), "month_day"),
        ("Christmas 1975.jpg", True, (1975, 12, 25), "christmas"),
        ("Merry Christmas 2019.jpg", True, (2019, 12, 25), "christmas"),
        ("June 2014 beach.jpg", True, (2014, 6, 1), "year_month"),
        ("Summer 08 lake.jpg", True, (2008, 7, 15), "season"),
        ("04.07.2003 party.jpg", True, (2003, 4, 7), "month_day_ambiguous"),
    ],
)
def test_written_dates(filename: str, descriptive: bool, ymd: tuple[int, int, int], rule: str) -> None:
    got = parse_written_date(filename, descriptive=descriptive, now=NOW)
    assert got is not None, filename
    assert (got.dt.year, got.dt.month, got.dt.day) == ymd
    assert got.rule == rule


@pytest.mark.parametrize(
    "filename,descriptive",
    [
        ("Christmas 1975.jpg", False),  # a year beside a word only counts in a real description
        ("IMG_2019 Hawaii.jpg", True),
        ("DSC_1975.jpg", True),
        ("Hockey 2010-11.jpg", True),
        ("Christmas.jpg", True),
    ],
)
def test_numbers_that_are_not_written_dates(filename: str, descriptive: bool) -> None:
    assert parse_written_date(filename, descriptive=descriptive, now=NOW) is None
