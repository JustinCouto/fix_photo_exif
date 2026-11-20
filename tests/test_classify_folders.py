"""Folder heuristics, date status precedence, proposal priority, apply defaults."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from photofix.classify import (
    CLUSTER_THRESHOLD,
    Proposal,
    assess,
    assess_date_status,
    build_folder_chain,
    decide_apply,
    propose_date,
)
from photofix.folders import classify_folder, derive_keywords, derive_title, is_junk_name
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
        ("iPhone Backup Photos", "iPhone Backup Photos"),  # mixed case preserved
        ("summer_vacation_2019", "Summer Vacation"),
        ("Beach-Day", "Beach Day"),
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


def test_priority_0_exif_createdate_beats_everything() -> None:
    got = propose_date(
        stored(create_date="2015:03:04 05:06:07", filename="2019-07-04.jpg"),
        folder_chain=folder_chain("2020-01-01 Trip"),
        now=NOW,
    )
    assert got.source == "exif_createdate"
    assert got.confidence == "high"
    assert got.dt == datetime(2015, 3, 4, 5, 6, 7)


def test_priority_1_filename_beats_folder_and_filesystem() -> None:
    got = propose_date(
        stored(filename="2019-07-04.jpg"), folder_chain=folder_chain("2020-01-01 Trip"), now=NOW
    )
    assert got.source == "filename"
    assert got.confidence == "high"


def test_priority_2_folder_beats_filesystem() -> None:
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=folder_chain("2020-01-01 Trip"), now=NOW)
    assert got.source == "folder"
    assert got.confidence == "medium"
    assert got.dt == datetime(2020, 1, 1, 12, 0, 0)


def test_priority_2_uses_the_nearest_dated_ancestor() -> None:
    chain = folder_chain("2020-06-06 Inner", "2015-01-01 Outer")
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=chain, now=NOW)
    assert got.dt == datetime(2020, 6, 6, 12, 0, 0)


def test_priority_3_filesystem_is_last_and_low_confidence() -> None:
    got = propose_date(stored(filename="IMG_0001.jpg"), folder_chain=folder_chain("Untitled"), now=NOW)
    assert got.source == "filesystem"
    assert got.confidence == "low"


def test_filesystem_uses_the_earlier_of_birthtime_and_mtime() -> None:
    """A copy pushes mtime forward; the earlier stamp is the closer bound."""
    early = datetime(2011, 1, 1, 0, 0, 0)
    late = datetime(2022, 1, 1, 0, 0, 0)
    got = propose_date(
        stored(filename="x.jpg", birthtime=late.timestamp(), mtime=early.timestamp()),
        folder_chain=[],
        now=NOW,
    )
    assert got.dt is not None and got.dt.year == 2011


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


def test_low_confidence_alone_does_not_default_to_apply() -> None:
    assert apply_for(proposal=Proposal(datetime(2019, 7, 4), "filesystem", "", "", "low")) is False


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


def test_folder_text_does_not_rescue_a_low_confidence_date() -> None:
    """`apply` approves the whole row, including the date.

    Returning True here because a title looked worthwhile would also write the
    guessed filesystem timestamp — which is how a run meant to add the title
    "Bulk Export" ends up stamping today's date onto every photo in it.
    """
    got = apply_for(
        proposal=Proposal(datetime(2019, 7, 4), "filesystem", "", "", "low"),
        inherit_title="Hawaii Trip",
    )
    assert got is False


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
