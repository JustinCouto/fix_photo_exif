"""Folder heuristics, date status precedence, proposal priority, apply defaults."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from photofix.classify import (
    CLUSTER_THRESHOLD,
    ClockSample,
    Proposal,
    assess,
    assess_date_status,
    build_folder_chain,
    clock_shifts,
    decide_apply,
    exact_folder_day,
    propose_date,
)
from photofix.folders import (
    classify_folder,
    derive_file_title,
    derive_keywords,
    derive_title,
    is_junk_name,
    is_placeholder_title,
)
from photofix.models import FolderProposal
from photofix.store import Cluster, StoredFile

NOW = datetime(2026, 9, 21, 12, 0, 0)


def stored(**kw: object) -> StoredFile:
    base: dict[str, object] = {
        "path": "C:/a/b.jpg",
        "real_path": "C:/a/b.jpg",
        "folder": "C:/a",
        "filename": "b.jpg",
        "ext": ".jpg",
        "size": 100,
        "mtime": datetime(2020, 5, 5, 9, 0, 0).timestamp(),
        "birthtime": datetime(2020, 5, 5, 8, 0, 0).timestamp(),
        "meta": {},
        "datetime_original": None,
        "create_date": None,
        "keywords": None,
        "title": None,
        "mime_type": "image/jpeg",
        "warning": None,
        "error": None,
    }
    base.update(kw)
    return StoredFile(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Folder classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "New Folder",
        "Untitled",
        "Photos",
        "DCIM",
        "100CANON",
        "101MSDCF",
        "Camera Roll",
        "misc",
        "2019",
        "2019-07-04",
        "New Folder (2)",
        "temp",
        "a",
        "",
    ],
)
def test_junk_folder_names(name: str) -> None:
    assert is_junk_name(name) is True


@pytest.mark.parametrize("name", ["Hawaii Trip", "2019-07-04 Family Reunion", "Grandma's 80th", "Maui"])
def test_real_folder_names_are_not_junk(name: str) -> None:
    assert is_junk_name(name) is False


def test_skip_suppresses_text_but_never_the_date() -> None:
    """A date-only folder is junk as a description and excellent as a date."""
    got = classify_folder("2019-07-04")
    assert got.skip is True
    assert got.title == "" and got.keywords == ""
    assert got.date == datetime(2019, 7, 4, 12, 0, 0), "skip must not blank the date"


def test_title_has_its_date_stripped() -> None:
    got = classify_folder("2019-07-04 Family Reunion")
    assert got.skip is False
    assert got.title == "Family Reunion"
    assert got.date == datetime(2019, 7, 4, 12, 0, 0)


@pytest.mark.parametrize(
    "name,title",
    [
        ("hawaii trip", "Hawaii Trip"),
        ("HAWAII TRIP", "Hawaii Trip"),
        ("iPhone Photos of Barney", "iPhone Photos of Barney"),  # mixed case preserved
        ("summer_vacation_2019", "Summer Vacation"),
        ("Beach-Day", "Beach Day"),
        ("Barney on christmas eve", "Barney on Christmas Eve"),
        ("the beach at maui", "The Beach at Maui"),
        ("trip to the lake and back", "Trip to the Lake and Back"),
        ("grandma's new year's eve", "Grandma's New Year's Eve"),
        ("McDonalds BBQ night", "McDonalds BBQ Night"),
        ("MOM & DAD", "Mom & Dad"),
        ("what we stood on", "What We Stood On"),
        ("noël à zürich", "Noël à Zürich"),
        ("cinco de mayo party", "Cinco de Mayo Party"),
    ],
)
def test_title_casing(name: str, title: str) -> None:
    assert classify_folder(name).title == title


def test_keywords_drop_stopwords_numbers_and_short_tokens() -> None:
    assert derive_keywords("The Beach at Maui in 2019") == "Beach;Maui"


def test_keywords_deduplicate_case_insensitively_keeping_first_spelling() -> None:
    assert derive_keywords("Maui maui MAUI Beach") == "Maui;Beach"


def test_keywords_use_semicolons_so_commas_in_values_survive() -> None:
    """A comma separator would split "Portland, OR" into two wrong keywords."""
    assert ";" in derive_keywords("Portland Oregon Trip")
    assert "," not in derive_keywords("Portland Oregon Trip")


def test_derive_title_strips_duplication_suffixes() -> None:
    assert derive_title("Hawaii Trip (2)") == "Hawaii Trip"
    assert derive_title("Hawaii Trip - Copy") == "Hawaii Trip"


@pytest.mark.parametrize(
    "name,title",
    [
        ("12.21 Barney on Christmas Eve", "Barney on Christmas Eve"),
        ("2021 Christmas Day", "Christmas Day"),
        ("Grandma's Birthday 2025", "Grandma's Birthday"),
        ("Christmas (2021)", "Christmas"),
        ("Christmas '21", "Christmas"),
        ("12-25-2021 Christmas Morning", "Christmas Morning"),
        ("December Snow", "December Snow"),
        ("Route 66 Trip", "Route 66 Trip"),
        ("Dec 21 Barney", "Barney"),
        ("Barney December 25th", "Barney"),
        ("2021, Christmas", "Christmas"),
        ("2019-07 Hawaii", "Hawaii"),
        ("21 Dec Party", "Party"),
        ("March 2019 Snow", "Snow"),
        ("Luke 2.5 Years Old", "Luke 2.5 Years Old"),
        ("Grandma's 80th Birthday", "Grandma's 80th Birthday"),
        ("1st birthday", "1st Birthday"),
    ],
)
def test_titles_carry_no_numeric_dates(name: str, title: str) -> None:
    assert classify_folder(name).title == title


@pytest.mark.parametrize(
    "name,title",
    [
        ("Mom & Dad 2019", "Mom & Dad"),
        ("Portland, OR", "Portland, OR"),
        ("Mr. Smith's Retirement", "Mr. Smith's Retirement"),
        ("Rock + Roll Night", "Rock + Roll Night"),
        ("Christmas.Party", "Christmas Party"),
    ],
)
def test_titles_keep_punctuation_that_belongs_to_the_words(name: str, title: str) -> None:
    assert classify_folder(name).title == title


@pytest.mark.parametrize(
    "filename",
    [
        "IMG_4471.jpg",
        "20205-01.jpg",
        "DSC_0001 (2).jpg",
        "PXL_20220101_123456789.jpg",
        "IMG-20190704-WA0001.jpg",
        "Screenshot 2021-01-01 at 10.00.00 AM.png",
        "Photo Dec 25.jpg",
        "IMG_E1234.HEIC",
        "FullSizeRender.jpg",
        "image copy.jpg",
        "3F2A1B9C-1234-4ABC-9DEF-0123456789AB.jpeg",
    ],
)
def test_generic_filenames_offer_no_title(filename: str) -> None:
    assert derive_file_title(filename) == ""


@pytest.mark.parametrize(
    "filename,title",
    [
        ("Barney opening presents.jpg", "Barney Opening Presents"),
        ("barney_opening_presents.jpg", "Barney Opening Presents"),
        ("IMG_1234 Beach.jpg", "Beach"),
        ("2021-12-25 New Year's Eve.jpg", "New Year's Eve"),
        ("Grandma (1).jpg", "Grandma"),
        ("Copy of Beach.jpg", "Beach"),
        ("Mom & Dad.jpg", "Mom & Dad"),
        ("IMG_1234 & Beach.jpg", "Beach"),
        ("Grandma's 80th.jpg", "Grandma's 80th"),
        ("Top 10 Moments.jpg", "Top 10 Moments"),
        ("Beach 3.jpg", "Beach"),
    ],
)
def test_descriptive_filenames_become_titles(filename: str, title: str) -> None:
    assert derive_file_title(filename) == title


@pytest.mark.parametrize(
    "filename",
    [
        "Canon.jpg",
        "Kodak 001.jpg",
        "Capture.PNG",
        "Snip 1.png",
        "Foto 001.jpg",
        "CRW_1234.jpg",
        "DCP_0001.jpg",
        "PXL_20190704_123456789-COLLAGE.jpg",
        "00000IMG_00000_BURST20190704123456_COVER.jpg",
    ],
)
def test_camera_and_app_names_offer_no_title(filename: str) -> None:
    assert derive_file_title(filename) == ""


def test_a_decimal_in_a_filename_title_is_kept() -> None:
    assert derive_file_title("Jake 2.5 years.jpg") == "Jake 2.5 Years"


@pytest.mark.parametrize(
    "name,title",
    [
        ("BBQ", "BBQ"),
        ("NYC 2019", "NYC"),
        ("LA 2019", "LA"),
        ("CD 1", "CD 1"),
        ("4TH OF JULY BBQ", "4th of July BBQ"),
        ("HAWAII TRIP", "Hawaii Trip"),
        ("Trip with Mom from Ohio", "Trip with Mom from Ohio"),
        ("Late 1960s", "Late 1960s"),
        ("Grandpa (1920-1995)", "Grandpa"),
        ("4th of July", "4th of July"),
        ("July 4th 2019", "4th of July"),
        ("Aunt May 80th Birthday", "Aunt May 80th Birthday"),
        ("Aunt May 2015", "Aunt May"),
        ("U.S. trip", "U.S. Trip"),
        ("Washington D.C. 2019", "Washington D.C."),
        ("Jake.Birthday.2019", "Jake Birthday"),
        ("Copy of Hawaii", "Hawaii"),
        ("3-2015 Disney", "Disney"),
        ("1/2 Marathon 2019", "1/2 Marathon"),
        ("3.5 Mile Hike 2019", "3.5 Mile Hike"),
        ("6-8 Grade 2019", "6-8 Grade"),
    ],
)
def test_more_folder_titles(name: str, title: str) -> None:
    assert classify_folder(name, now=NOW).title == title


@pytest.mark.parametrize(
    "name",
    [
        "Photos from 2015",
        "Misc 2019",
        "Backup 2012",
        "Camera Roll 2019",
        "My Photos 2005",
        "Export 2019-07-04",
        "Untitled 2",
        "Takeout",
        "Google Photos",
        "1990s",
        "Circa 1975",
        "1975?",
        "~1975",
    ],
)
def test_names_that_are_junk_once_the_date_is_gone(name: str) -> None:
    got = classify_folder(name, now=NOW)
    assert got.title == "" and got.skip


@pytest.mark.parametrize(
    "title,placeholder",
    [
        ("OLYMPUS DIGITAL CAMERA", True),
        ("KODAK Digital Still Camera", True),
        ("Dad with his new digital camera", False),
        ("Grandpa's Kodak digital still camera collection", False),
    ],
)
def test_camera_boilerplate_is_the_whole_title_not_part_of_it(title: str, placeholder: bool) -> None:
    assert is_placeholder_title(title) is placeholder


def test_inner_generic_folders_carry_their_parents_title(tmp_path: Path) -> None:
    from photofix.scan import _build_folder_cache, _date_found

    root = tmp_path
    day = root / "Disney 2018" / "Day 1"
    bare = root / "2019" / "Day 2"
    cache = _build_folder_cache({str(day): 3, str(bare): 2}, root)
    assert cache[str(day)].title == "Disney Day 1"
    assert cache[str(bare)].title == "Day 2", "no titled parent, nothing to add"
    assert _date_found(classify_folder("Grandpa (1920-1995)")) == "spans 1920-1995: no date"
    assert _date_found(classify_folder("Christmas")) == "Christmas (year from a parent folder)"


# ---------------------------------------------------------------------------
# date_status precedence
# ---------------------------------------------------------------------------


def test_missing_date() -> None:
    status, flags = assess_date_status(None, cluster=None, now=NOW)
    assert status == "missing"
    assert "no_exif_date" in flags


def test_zero_date_is_missing_not_implausible() -> None:
    status, _ = assess_date_status("0000:00:00 00:00:00", cluster=None, now=NOW)
    assert status == "missing"


@pytest.mark.parametrize(
    "value,flag",
    [
        ("1899:12:31 00:00:00", "pre_1900"),
        ("2030:01:01 00:00:00", "future"),
        ("2002:12:08 12:00:00", "junk_default"),
        ("1980:01:01 00:00:00", "junk_default"),
    ],
)
def test_implausible_dates(value: str, flag: str) -> None:
    status, flags = assess_date_status(value, cluster=None, now=NOW)
    assert status == "implausible"
    assert flag in flags


def test_clustered_date() -> None:
    cluster = Cluster("2013:01:01 00:00:00", 40, 12)
    status, flags = assess_date_status("2013:01:01 00:00:00", cluster=cluster, now=NOW)
    assert status == "clustered"
    assert "cluster" in flags
    assert "burst_suspect" not in flags


def test_single_folder_cluster_is_flagged_as_a_probable_burst() -> None:
    """40 frames in one folder is a camera burst, not damage."""
    cluster = Cluster("2013:01:01 00:00:00", 40, 1)
    status, flags = assess_date_status("2013:01:01 00:00:00", cluster=cluster, now=NOW)
    assert status == "clustered"
    assert "burst_suspect" in flags


def test_implausible_outranks_clustered() -> None:
    """The implausibility explains the clustering; reporting 'clustered' buries it."""
    cluster = Cluster("1980:01:01 00:00:00", 500, 90)
    status, _ = assess_date_status("1980:01:01 00:00:00", cluster=cluster, now=NOW)
    assert status == "implausible"


def test_a_good_date_below_the_cluster_threshold_is_ok() -> None:
    cluster = Cluster("2013:01:01 00:00:00", CLUSTER_THRESHOLD - 1, 3)
    status, _ = assess_date_status("2013:01:01 00:00:00", cluster=cluster, now=NOW)
    assert status == "ok"


# ---------------------------------------------------------------------------
# Proposal priority
# ---------------------------------------------------------------------------


def folder_chain(*names: str) -> list[tuple[str, FolderProposal]]:
    return [(f"C:/{n}", classify_folder(n)) for n in names]


def test_createdate_never_beats_the_filename_or_a_disagreeing_folder() -> None:
    """On a scanned print, CreateDate is the day of the scan."""
    got = propose_date(
        stored(create_date="2015:03:04 05:06:07", filename="2019-07-04.jpg"),
        folder_chain=folder_chain("2020-01-01 Trip"),
        now=NOW,
    )
    assert got.source == "filename"
    got = propose_date(
        stored(create_date="2005:03:02 10:00:00", filename="scan0001.jpg"),
        folder_chain=dated_chain("Christmas 1975", "Scans"),
        now=NOW,
    )
    assert got.source == "folder"
    assert got.dt == datetime(1975, 12, 25, 12)


def test_createdate_refines_a_folder_date_it_agrees_with() -> None:
    got = propose_date(
        stored(create_date="2019:07:04 15:22:01", filename="IMG_0001.jpg"),
        folder_chain=dated_chain("2019 Hawaii"),
        now=NOW,
    )
    assert got.source == "exif_createdate"
    assert got.confidence == "high"
    assert got.dt == datetime(2019, 7, 4, 15, 22, 1)


def test_createdate_is_used_when_nothing_else_has_a_date() -> None:
    got = propose_date(
        stored(create_date="2015:03:04 05:06:07", filename="IMG_0001.jpg"), folder_chain=[], now=NOW
    )
    assert got.source == "exif_createdate"
    assert got.confidence == "medium"


def test_a_good_xmp_date_beats_a_junk_exif_one() -> None:
    meta = {
        "EXIF:DateTimeOriginal": "2002:12:08 12:00:00",
        "XMP:DateCreated": "1984:12:25",
        "IPTC:DateCreated": "1984:12:25",
    }
    s = stored(datetime_original="2002:12:08 12:00:00", meta=meta, filename="scan.jpg")
    got = propose_date(s, folder_chain=dated_chain("Old Scans 1985"), now=NOW)
    assert got.source == "existing_metadata"
    assert got.dt == datetime(1984, 12, 25, 12)


def test_a_folder_spanning_years_stops_the_search_for_a_date() -> None:
    s = stored(filename="IMG_0001.jpg")
    got = propose_date(s, folder_chain=dated_chain("Christmas", "Grandpa (1920-1995)", "2005"), now=NOW)
    assert got.dt is None and got.rule == "multi_year"
    s = stored(filename="IMG_0001.jpg", create_date="1960:07:04 10:00:00")
    got = propose_date(s, folder_chain=dated_chain("Grandpa (1920-1995)"), now=NOW)
    assert got.source == "exif_createdate", "a CreateDate inside the span is consistent with it"


def test_a_coarser_folder_date_does_not_replace_a_clustered_time_by_default() -> None:
    s = stored(datetime_original="2019:07:04 15:00:00", filename="IMG_0001.jpg")
    verdict = assess(
        s,
        folder_chain=dated_chain("2019 Party"),
        clusters={"2019:07:04 15:00:00": Cluster("2019:07:04 15:00:00", 45, 3)},
        now=NOW,
    )
    assert verdict.proposed_date == "2019:01:01 12:00:00"
    assert "coarser_than_existing" in verdict.date_flags
    assert verdict.apply is False


@pytest.mark.parametrize("value", ["1970:01:01 09:00:00", "1904:01:01 00:00:00", "1980:01:01 08:30:00"])
def test_clock_reset_days_are_junk_at_any_time(value: str) -> None:
    status, flags = assess_date_status(value, cluster=None, now=NOW)
    assert status == "implausible" and "junk_default" in flags


def test_priority_1_filename_beats_folder() -> None:
    got = propose_date(
        stored(filename="2019-07-04.jpg"), folder_chain=folder_chain("2020-01-01 Trip"), now=NOW
    )
    assert got.source == "filename"
    assert got.confidence == "high"


def test_priority_2_folder() -> None:
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=folder_chain("2020-01-01 Trip"), now=NOW)
    assert got.source == "folder"
    assert got.confidence == "medium"
    assert got.dt == datetime(2020, 1, 1, 12, 0, 0)


@pytest.mark.parametrize(
    "name,expected",
    [
        ("2021 Christmas Day", datetime(2021, 12, 25, 12)),
        ("Christmas Eve 2020", datetime(2020, 12, 24, 12)),
        ("Thanksgiving 2021", datetime(2021, 11, 25, 12)),
        ("12-25-2021 Christmas Morning", datetime(2021, 12, 25, 12)),
        ("2021-12-24 Christmas", datetime(2021, 12, 24, 12)),  # a full date is never overridden
        ("2019 Hawaii", datetime(2019, 1, 1, 12)),
    ],
)
def test_a_folder_combines_its_own_year_with_a_month_day(name: str, expected: datetime) -> None:
    got = classify_folder(name, now=NOW)
    assert got.date == expected
    assert got.partial is None


def test_a_month_day_without_a_year_waits_for_a_parent() -> None:
    got = classify_folder("12.21 Barney on Christmas Eve", now=NOW)
    assert got.date is None
    assert got.partial is not None


def dated_chain(*names: str) -> list[tuple[str, FolderProposal]]:
    return [(f"C:/{n}", classify_folder(n, now=NOW)) for n in names]


def test_a_month_day_takes_its_year_from_a_parent_folder() -> None:
    got = propose_date(
        stored(filename="IMG_0001.jpg"),
        folder_chain=dated_chain("12.21 Barney on Christmas Eve", "2021"),
        now=NOW,
    )
    assert got.dt == datetime(2021, 12, 21, 12)
    assert got.rule == "month_day+folder_year"


def test_a_bare_year_folder_takes_its_day_from_a_holiday_folder_above_it() -> None:
    got = propose_date(
        stored(filename="IMG_0001.jpg"), folder_chain=dated_chain("2021", "Christmas"), now=NOW
    )
    assert got.dt == datetime(2021, 12, 25, 12)


def test_a_holiday_folder_only_dates_a_year_folder_directly_inside_it() -> None:
    """Christmas/Hawaii/2021 is a Hawaii trip, not Christmas Day."""
    got = propose_date(
        stored(filename="IMG_0001.jpg"), folder_chain=dated_chain("2021", "Hawaii", "Christmas"), now=NOW
    )
    assert got.dt == datetime(2021, 1, 1, 12)


def test_a_burst_is_not_redated_by_default() -> None:
    """One folder, one second, many frames: the timestamp is probably right."""
    t = "2019:06:01 10:00:00"
    got = assess(
        stored(datetime_original=t, create_date=t, filename="IMG_0001.jpg"),
        folder_chain=dated_chain("2019"),
        clusters={t: Cluster(t, 40, 1)},
        now=NOW,
    )
    assert "burst_suspect" in got.date_flags
    assert got.proposed_date == "2019:01:01 12:00:00", "still shown for review"
    assert got.apply is False


def test_a_multi_folder_cluster_is_still_redated_by_default() -> None:
    t = "2019:06:01 10:00:00"
    got = assess(
        stored(datetime_original=t, create_date=t, filename="2019-07-04.jpg"),
        folder_chain=[],
        clusters={t: Cluster(t, 400, 30)},
        now=NOW,
    )
    assert got.proposed_date == "2019:07:04 12:00:00"
    assert got.apply is True


def test_a_rescan_after_apply_does_not_propose_the_date_already_there() -> None:
    """Forty photos given 2020-12-25 noon cluster; re-proposing it fixes nothing."""
    t = "2020:12:25 12:00:00"
    got = assess(
        stored(datetime_original=t, filename="IMG_0001.jpg", title="Christmas Day", keywords="Christmas;Day"),
        folder_chain=dated_chain("2020 Christmas Day"),
        clusters={t: Cluster(t, 60, 3)},
        now=NOW,
    )
    assert got.date_status == "clustered"
    assert got.proposed_date == ""
    assert "proposal_matches_existing" in got.date_flags
    assert "no_candidate" not in got.date_flags
    assert got.apply is False


def test_a_file_exiftool_could_not_read_gets_no_proposal_and_no_approval() -> None:
    """Its date and keywords are unknown, not missing."""
    got = assess(
        stored(filename="IMG_1.jpg", error="Error opening file"),
        folder_chain=dated_chain("Picnic 2012"),
        clusters={},
        now=NOW,
    )
    assert got.proposed_date == ""
    assert got.apply is False
    assert "exiftool_error" in got.date_flags
    assert "no_candidate" not in got.date_flags


def test_a_month_year_folder_dates_to_the_first_and_is_flagged() -> None:
    got = assess(
        stored(filename="IMG_1.jpg"), folder_chain=dated_chain("2019-07 Hawaii"), clusters={}, now=NOW
    )
    assert got.proposed_date == "2019:07:01 12:00:00"
    assert "month_only" in got.date_flags
    assert "year_only" not in got.date_flags


def test_a_good_date_is_not_flagged_for_matching_its_folder() -> None:
    got = assess(
        stored(datetime_original="2020:12:25 12:00:00", filename="IMG_0001.jpg"),
        folder_chain=dated_chain("2020 Christmas Day"),
        clusters={},
        now=NOW,
    )
    assert got.date_status == "ok"
    assert "proposal_matches_existing" not in got.date_flags


def test_a_holiday_with_no_year_anywhere_proposes_nothing() -> None:
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=dated_chain("Christmas"), now=NOW)
    assert got.dt is None


def test_a_combined_folder_date_is_not_flagged_year_only() -> None:
    got = assess(
        stored(filename="IMG_0001.jpg"), folder_chain=dated_chain("2021 Christmas Day"), clusters={}, now=NOW
    )
    assert got.proposed_date == "2021:12:25 12:00:00"
    assert "year_only" not in got.date_flags


@pytest.mark.parametrize(
    "text,placeholder",
    [
        ("", True),
        ("OLYMPUS DIGITAL CAMERA", True),
        ("SONY DSC", True),
        ("KONICA MINOLTA DIGITAL CAMERA", True),
        ("IMG_1234", True),
        ("Grandma's 80th", False),
        ("Hawaii Trip", False),
    ],
)
def test_placeholder_titles(text: str, placeholder: bool) -> None:
    assert is_placeholder_title(text) is placeholder


def test_a_placeholder_existing_title_does_not_block_a_folder_title() -> None:
    assert apply_for(date_status="ok", inherit_title="Hawaii Trip", existing_title="OLYMPUS DIGITAL CAMERA")


def test_a_title_can_fill_a_gap_beside_existing_keywords() -> None:
    assert apply_for(date_status="ok", inherit_title="Hawaii Trip", existing_keywords="beach") is True


def test_priority_2_uses_the_nearest_dated_ancestor() -> None:
    chain = folder_chain("2020-06-06 Inner", "2015-01-01 Outer")
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=chain, now=NOW)
    assert got.dt == datetime(2020, 6, 6, 12, 0, 0)


def test_filesystem_dates_are_never_used() -> None:
    """Created/modified times usually record the last copy, not the photo."""
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=folder_chain("Untitled"), now=NOW)
    assert got.dt is None and got.source is None


def test_createdate_equal_to_a_clustered_date_is_not_proposed() -> None:
    """A bulk edit stamps both tags; proposing CreateDate would re-approve the damage."""
    bad = "2015:06:01 10:00:00"
    got = propose_date(
        stored(datetime_original=bad, create_date=bad, filename="IMG_0001.jpg"),
        folder_chain=folder_chain("2019-07-04 Trip"),
        clusters={bad: Cluster(bad, 400, 30)},
        now=NOW,
    )
    assert got.source == "folder"
    assert got.dt == datetime(2019, 7, 4, 12, 0, 0)


def test_createdate_that_is_itself_clustered_is_not_proposed() -> None:
    bad = "2015:06:01 10:00:00"
    got = propose_date(
        stored(create_date=bad, filename="IMG_0001.jpg"),
        folder_chain=[],
        clusters={bad: Cluster(bad, 400, 30)},
        now=NOW,
    )
    assert got.dt is None


def test_junk_createdate_is_not_used_as_a_source() -> None:
    got = propose_date(
        stored(create_date="2002:12:08 12:00:00", filename="2019-07-04.jpg"),
        folder_chain=[],
        now=NOW,
    )
    assert got.source == "filename"


def test_no_candidate_at_all() -> None:
    got = propose_date(stored(filename="IMG_0001.jpg", birthtime=None, mtime=0.0), folder_chain=[], now=NOW)
    assert got.dt is None and got.source is None


# ---------------------------------------------------------------------------
# apply defaults
# ---------------------------------------------------------------------------


def apply_for(**kw: object) -> bool:
    base: dict[str, object] = {
        "date_status": "missing",
        "proposal": Proposal(datetime(2019, 7, 4), "filename", "", "", "high"),
        "writable": True,
        "inherit_title": "",
        "inherit_keywords": "",
        "existing_title": "",
        "existing_keywords": "",
    }
    base.update(kw)
    return decide_apply(**base)  # type: ignore[arg-type]


def test_high_and_medium_confidence_default_to_apply() -> None:
    assert apply_for() is True
    assert apply_for(proposal=Proposal(datetime(2019, 7, 4), "folder", "", "", "medium")) is True


def test_unwritable_never_defaults_to_apply() -> None:
    assert apply_for(writable=False) is False
    assert apply_for(writable=False, inherit_title="Trip") is False


def test_ok_date_with_folder_text_and_nothing_to_clobber_applies() -> None:
    """Otherwise a whole archive of correctly-dated photos gets no titles at all."""
    assert apply_for(date_status="ok", inherit_title="Hawaii Trip", inherit_keywords="Hawaii") is True


def test_ok_date_never_clobbers_an_existing_title() -> None:
    assert apply_for(date_status="ok", inherit_title="Hawaii Trip", existing_title="Real Title") is False
    assert apply_for(date_status="ok", inherit_keywords="Hawaii", existing_keywords="beach") is False


def test_ok_date_with_no_folder_text_does_nothing() -> None:
    assert apply_for(date_status="ok") is False


def test_folder_text_does_apply_when_there_is_no_date_to_write() -> None:
    """No candidate means proposed_date is blank, so the row is text-only."""
    got = apply_for(proposal=Proposal(None, None, "", "", None), inherit_title="Hawaii Trip")
    assert got is True


def test_a_text_only_row_still_respects_existing_metadata() -> None:
    got = apply_for(
        proposal=Proposal(None, None, "", "", None),
        inherit_title="Hawaii Trip",
        existing_title="Already Titled",
    )
    assert got is False


def test_bare_year_folder_dates_are_flagged_as_year_only() -> None:
    """2019-01-01 renders as a specific day but is only accurate to the year."""
    got = assess(stored(filename="IMG_0001.jpg"), folder_chain=folder_chain("2019"), clusters={}, now=NOW)
    assert got.proposed_date == "2019:01:01 12:00:00"
    assert "year_only" in got.date_flags


# ---------------------------------------------------------------------------
# Full assessment
# ---------------------------------------------------------------------------


def test_ok_files_get_a_blank_proposed_date() -> None:
    """Blank means "leave it alone" to both exiftool and the reviewer."""
    got = assess(
        stored(datetime_original="2019:07:04 12:00:00", filename="2011-01-01.jpg"),
        folder_chain=[],
        clusters={},
        now=NOW,
    )
    assert got.date_status == "ok"
    assert got.proposed_date == ""
    assert got.date_source == "" and got.confidence == ""


def test_missing_date_gets_a_proposal_and_flags() -> None:
    got = assess(
        stored(filename="2019-07-04.jpg"),
        folder_chain=folder_chain("Hawaii Trip"),
        clusters={},
        now=NOW,
    )
    assert got.date_status == "missing"
    assert got.proposed_date == "2019:07:04 12:00:00"
    assert got.date_source == "filename"
    assert got.inherit_title == "Hawaii Trip"
    assert got.inherit_keywords == "Hawaii;Trip"
    assert got.apply is True


def test_a_descriptive_filename_is_offered_alongside_the_folder_title() -> None:
    got = assess(
        stored(datetime_original="2019:07:04 12:00:00", filename="Barney opening presents.jpg"),
        folder_chain=folder_chain("Grandma's Birthday 2025"),
        clusters={},
        now=NOW,
    )
    assert got.filename_title == "Barney Opening Presents"
    assert got.inherit_title == "Grandma's Birthday"
    assert got.apply is True


def test_gif_is_writable_with_xmp_and_unwritable_without() -> None:
    """Verified in Phase 0: GIF has no EXIF container and rejects IPTC."""
    with_xmp = assess(stored(filename="a.gif", ext=".gif"), folder_chain=[], clusters={}, now=NOW)
    assert with_xmp.writable is True

    without = assess(
        stored(filename="a.gif", ext=".gif"), folder_chain=[], clusters={}, now=NOW, xmp_enabled=False
    )
    assert without.writable is False
    assert "unwritable" in without.date_flags
    assert without.apply is False


def test_bmp_is_never_writable() -> None:
    got = assess(stored(filename="a.bmp", ext=".bmp"), folder_chain=[], clusters={}, now=NOW)
    assert got.writable is False


def test_mime_mismatch_is_flagged() -> None:
    got = assess(
        stored(filename="a.jpg", ext=".jpg", mime_type="image/png"),
        folder_chain=[],
        clusters={},
        now=NOW,
    )
    assert "mime_mismatch" in got.date_flags


def test_matching_mime_is_not_flagged() -> None:
    got = assess(stored(mime_type="image/jpeg"), folder_chain=[], clusters={}, now=NOW)
    assert "mime_mismatch" not in got.date_flags


def test_cluster_columns_are_populated() -> None:
    clusters = {"2013:01:01 00:00:00": Cluster("2013:01:01 00:00:00", 40, 12)}
    got = assess(
        stored(datetime_original="2013:01:01 00:00:00", filename="2019-07-04.jpg"),
        folder_chain=[],
        clusters=clusters,
        now=NOW,
    )
    assert got.date_status == "clustered"
    assert got.cluster_size == "40"
    assert got.cluster_folders == "12"


def test_exiftool_warnings_are_surfaced_as_flags() -> None:
    got = assess(stored(warning="Truncated file"), folder_chain=[], clusters={}, now=NOW)
    assert "exiftool_warning" in got.date_flags


# ---------------------------------------------------------------------------
# Folder chain
# ---------------------------------------------------------------------------


def test_folder_chain_runs_inner_to_outer_and_excludes_root() -> None:
    root = str(Path("C:/archive"))
    cache = {
        str(Path("C:/archive")): classify_folder("archive"),
        str(Path("C:/archive/2019")): classify_folder("2019"),
        str(Path("C:/archive/2019/Hawaii Trip")): classify_folder("Hawaii Trip"),
    }
    chain = build_folder_chain(str(Path("C:/archive/2019/Hawaii Trip")), root, cache)
    names = [Path(p).name for p, _ in chain]
    assert names == ["Hawaii Trip", "2019"]
    assert "archive" not in names, "root describes the archive, not any photo in it"


# ---------------------------------------------------------------------------
# Names from phones, apps, exports and backups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Favorites",
        "Selects",
        "Lightroom Exports",
        "Sent",
        "Messenger",
        "Snapchat Memories",
        "Facebook Photos",
        "Old Computer",
        "Backup Drive",
        "Videos",
        "100_PANA",
        "100NCD90",
        "100EOS5D",
        "IMG_0001-0500",
        "takeout-20200101T120000Z-001",
        "Backup Jan 2012",
        "March 2019",
        "Jan 2010",
        "12-24-2021 to 12-26-2021",
    ],
)
def test_names_that_describe_no_photo(name: str) -> None:
    proposal = classify_folder(name, now=NOW)
    assert proposal.skip and proposal.title == ""


@pytest.mark.parametrize("name", ["Backup Jan 2012", "takeout-20200101T120000Z-001"])
def test_the_day_an_export_was_made_is_not_the_day_a_photo_was_taken(name: str) -> None:
    assert classify_folder(name, now=NOW).date is None


@pytest.mark.parametrize(
    "name,title,keywords",
    [
        ("Summer 2008", "Summer", "Summer"),
        ("Xmas 99", "Xmas", "Xmas"),
        ("XMAS 2004", "Xmas", "Xmas"),
        ("NYE 2009", "NYE", "NYE"),
        ("Hockey 2010-11", "Hockey", "Hockey"),
        ("Hawaii Trip June 2014", "Hawaii Trip", "Hawaii;Trip"),
        ("Christmas Dec 2010", "Christmas", "Christmas"),
        ("2021-12-24 to 2021-12-26 Christmas", "Christmas", "Christmas"),
        ("nyc trip 2019", "NYC Trip", "NYC;Trip"),
        ("D-Day Memorial", "D-Day Memorial", "D-Day;Memorial"),
        ("Mother-in-Law Visit 2015", "Mother-in-Law Visit", "Mother-in-Law;Visit"),
        ("hawaii-trip-2019", "Hawaii Trip", "Hawaii;Trip"),
        ("Jean-Luc's Party", "Jean-Luc's Party", "Jean-Luc's;Party"),
        ("St. Louis Arch 2012", "St. Louis Arch", "St. Louis;Arch"),
        ("Christmas Eve 2019", "Christmas Eve", "Christmas Eve"),
        ("New Year's Eve 2019", "New Year's Eve", "New Year's Eve"),
        ("Mom Turns 50", "Mom Turns 50", "Mom"),
        ("Fall Festival 2019", "Fall Festival", "Fall;Festival"),
    ],
)
def test_typed_folder_titles_and_keywords(name: str, title: str, keywords: str) -> None:
    proposal = classify_folder(name, now=NOW)
    assert (proposal.title, proposal.keywords) == (title, keywords)


@pytest.mark.parametrize(
    "filename,title",
    [
        ("Emma age 2.jpg", "Emma Age 2"),
        ("Beach Day 3.jpg", "Beach Day 3"),
        ("Beach 3.jpg", "Beach"),
        ("Image from iOS.jpg", ""),
        ("attachment.jpg", ""),
        ("maxresdefault.jpg", ""),
        ("New Doc 2019-01-01.jpg", ""),
        ("CamScanner 07-04-2019 15.30.jpg", ""),
        ("Enhanced NR.jpg", ""),
        ("Messenger Creation.jpg", ""),
        ("Screenshot_20190704-123045_com.facebook.katana.png", ""),
    ],
)
def test_app_filenames_and_numbered_titles(filename: str, title: str) -> None:
    assert derive_file_title(filename) == title


# ---------------------------------------------------------------------------
# Dates from typed names, weighed against the photo's own
# ---------------------------------------------------------------------------


def _chain(*names: str) -> list[tuple[str, FolderProposal]]:
    out, path = [], ""
    for name in names:
        path += "/" + name
        out.append((path, classify_folder(name, now=NOW)))
    return list(reversed(out))


def _named(filename: str, **kw: object) -> StoredFile:
    return stored(path=f"/a/{filename}", real_path=f"/a/{filename}", folder="/a", filename=filename, **kw)


@pytest.mark.parametrize(
    "filename,folders,date,source",
    [
        ("1012345678.jpg", ("Christmas 1985",), "1985:12:25 12:00:00", "folder"),  # upload time outside 1985
        ("Snapchat-1187654321.jpg", ("2016 Snapchat Saves",), "2016:01:01 12:00:00", "folder"),
        ("FB_IMG_1563012345678.jpg", ("2019",), "2019:07:13 03:05:45", "filename"),
        ("Mom and Dad 12-25-1975.jpg", ("Scans",), "1975:12:25 12:00:00", "filename"),
        ("Christmas 1975.jpg", ("Scans",), "1975:12:25 12:00:00", "filename"),
        ("1975 wedding.jpg", ("Wedding 6-14-1975",), "1975:06:14 12:00:00", "folder"),
        ("IMG_6.jpg", ("Christmas Cards", "2019", "Hawaii"), "2019:01:01 12:00:00", "folder"),
        ("IMG_7.jpg", ("2019-07", "Christmas in July"), "2019:07:01 12:00:00", "folder"),
        ("IMG_8.jpg", ("Christmas", "2021"), "2021:12:25 12:00:00", "folder"),
    ],
)
def test_typed_dates_take_their_place_in_the_priority(
    filename: str, folders: tuple[str, ...], date: str, source: str
) -> None:
    result = assess(
        _named(filename, create_date="2015:03:02 10:11:12"),
        folder_chain=_chain(*folders),
        clusters={},
        now=NOW,
    )
    assert (result.proposed_date, result.date_source) == (date, source)


def test_an_upload_time_is_flagged() -> None:
    result = assess(_named("FB_IMG_1563012345678.jpg"), folder_chain=_chain("2019"), clusters={}, now=NOW)
    assert "download_time" in result.date_flags.split(",")


def test_an_ambiguous_folder_date_waits_for_review() -> None:
    result = assess(_named("IMG_1.jpg"), folder_chain=_chain("04.07.2003"), clusters={}, now=NOW)
    assert "ambiguous_day_month" in result.date_flags.split(",") and not result.apply


def test_a_season_lands_mid_season_and_is_flagged() -> None:
    result = assess(_named("IMG_9.jpg"), folder_chain=_chain("Summer 2008"), clusters={}, now=NOW)
    assert result.proposed_date == "2008:07:15 12:00:00"
    assert "season_only" in result.date_flags.split(",") and result.apply


@pytest.mark.parametrize(
    "dto,folders,source",
    [
        ("1985:06:01 12:00:00", ("Beach Trip",), "exif_createdate"),
        ("2019:07:04 15:30:12", ("Summer 2018",), "folder"),
    ],
)
def test_a_clustered_date_replaced_by_a_guess_waits_for_review(
    dto: str, folders: tuple[str, ...], source: str
) -> None:
    result = assess(
        _named(
            "scan0001.jpg",
            datetime_original=dto,
            create_date="2015:03:02 10:11:12" if source != "folder" else None,
        ),
        folder_chain=_chain(*folders),
        clusters={dto: Cluster(dto, 30, 5)},
        now=NOW,
    )
    assert result.date_source == source
    assert "clustered_review" in result.date_flags.split(",") and not result.apply


def test_a_believable_date_outside_its_folder_is_flagged_not_changed() -> None:
    result = assess(
        _named("DSC_0001.JPG", datetime_original="2001:01:01 00:05:12"),
        folder_chain=_chain("Hawaii 2014"),
        clusters={},
        now=NOW,
    )
    assert result.date_status == "ok" and result.proposed_date == ""
    assert "folder_disagrees" in result.date_flags.split(",")


def test_a_date_a_few_hours_ahead_is_a_time_zone_not_the_future() -> None:
    result = assess(
        _named("IMG_11.jpg", datetime_original="2026:09:22 08:00:00"), folder_chain=[], clusters={}, now=NOW
    )
    assert result.date_status == "ok"


# ---------------------------------------------------------------------------
# A holiday that names a different month
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,ymd,rule,title",
    [
        ("Guitar Christmas - January 2011", (2011, 1, 1), "year_month", "Guitar Christmas"),
        ("Christmas in July 2019", (2019, 7, 1), "year_month", "Christmas in July"),
        ("Christmas - December 2004", (2004, 12, 25), "christmas", "Christmas"),
        ("Thanksgiving November 2015", (2015, 11, 26), "thanksgiving", "Thanksgiving"),
    ],
)
def test_a_stated_month_beats_a_holiday_in_another_month(
    name: str, ymd: tuple[int, int, int], rule: str, title: str
) -> None:
    proposal = classify_folder(name, now=NOW)
    assert proposal.date is not None
    assert (proposal.date.year, proposal.date.month, proposal.date.day) == ymd
    assert (proposal.date_rule, proposal.title) == (rule, title)


@pytest.mark.parametrize("name", ["Christmas in July", "Guitar Christmas - January"])
def test_a_holiday_in_another_month_is_not_passed_to_a_parent_year(name: str) -> None:
    assert classify_folder(name, now=NOW).partial is None


def test_a_person_named_for_a_month_does_not_cancel_a_holiday() -> None:
    assert classify_folder("Aunt May Christmas", now=NOW).partial is not None


# ---------------------------------------------------------------------------
# Wrong camera clocks
# ---------------------------------------------------------------------------


def test_an_exact_folder_day_is_trusted_when_its_parent_agrees() -> None:
    day = exact_folder_day(_chain("2016", "03-27-2016 - Easter"), now=NOW)
    assert day == datetime(2016, 3, 27)


@pytest.mark.parametrize(
    "folders",
    [
        ("2018", "04-20-2019 - Photo Shoot"),  # one of the two is a typo
        ("Summer 2016",),  # not an exact day
        ("2016",),
        ("Grandpa (1920-1940)", "12-25-1950"),
    ],
)
def test_no_exact_day_when_folders_disagree_or_are_vague(folders: tuple[str, ...]) -> None:
    chain = _chain(*folders)
    assert exact_folder_day(chain, now=NOW) is None


def _samples(*taken: str, camera: str = "X-T1", day: datetime = datetime(2016, 3, 27)) -> list[ClockSample]:
    return [
        ClockSample(str(i), ("/2016/Easter", camera), datetime.strptime(t, "%Y-%m-%d %H:%M"), day)
        for i, t in enumerate(taken)
    ]


def test_a_camera_far_off_its_folders_day_is_shifted_by_whole_days() -> None:
    shifts = clock_shifts(_samples("2015-05-02 10:00", "2015-05-02 23:33", "2015-05-03 00:10"))
    assert set(shifts.values()) == {330}


@pytest.mark.parametrize(
    "taken",
    [
        ("2015-05-02 10:00", "2015-05-02 11:00"),  # too few to judge a clock by
        ("2016-03-26 22:00", "2016-03-27 10:00", "2016-03-27 11:00"),  # a day off: time zone or late folder
        ("2016-02-20 10:00", "2016-03-02 10:00", "2016-03-27 11:00"),  # some photos agree with the folder
        ("2015-05-02 10:00", "2015-06-02 10:00", "2015-07-02 10:00"),  # spread over months: not one event
    ],
)
def test_no_shift_without_a_clear_wrong_clock(taken: tuple[str, ...]) -> None:
    assert clock_shifts(_samples(*taken)) == {}


def test_cameras_in_one_folder_are_judged_separately() -> None:
    wrong = _samples("2015-05-02 10:00", "2015-05-02 11:00", "2015-05-02 12:00", camera="X-T1")
    right = [
        ClockSample(f"r{i}", ("/2016/Easter", "iPhone"), datetime(2016, 3, 27, 10 + i), datetime(2016, 3, 27))
        for i in range(3)
    ]
    assert set(clock_shifts(wrong + right)) == {"0", "1", "2"}
