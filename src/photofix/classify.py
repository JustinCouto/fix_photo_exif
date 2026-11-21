"""Decide each file's date status and what to propose for it.

Two separable judgments that are easy to conflate:

* `date_status` describes what is *wrong* with the existing date, if anything.
* the proposal describes what we would put there instead, and how much we trust it.

A file can be `ok` and still get a proposal (a folder-derived title), and it can
be `missing` with no proposal at all (nothing in the EXIF data, filename, or
folder was usable). The `apply` default has to handle both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from .datenames import (
    CLOCK_RESET_DAYS,
    COARSE_FOLDER_RULES,
    EPOCH_RULES,
    PartialDate,
    date_window,
    parse_filename_date,
    parse_written_date,
)
from .folders import derive_file_title, is_placeholder_title
from .formats import is_writable
from .models import Confidence, DateCandidate, DateSource, DateStatus, FolderProposal
from .store import Cluster, StoredFile, normalize_timestamp

#: Files sharing an exact second with this many others are suspect.
CLUSTER_THRESHOLD: Final = 20

MIN_PLAUSIBLE_YEAR: Final = 1900

#: Timestamps that are technically valid and semantically meaningless: camera
#: firmware defaults, scanner defaults, and the Unix epoch. Compared against the
#: normalized (subsecond- and timezone-stripped) value.
JUNK_TIMESTAMPS: Final[frozenset[str]] = frozenset(
    {
        "0000:00:00 00:00:00",
        "1970:01:01 00:00:00",
        "1980:01:01 00:00:00",
        "1980:01:01 12:00:00",
        "2000:01:01 00:00:00",
        "2002:12:08 12:00:00",  # a very common flatbed-scanner default
        "2003:01:01 00:00:00",
        "2008:01:01 00:00:00",
    }
)
#: Clock-reset days, at whatever time the camera ran on from: "1970:01:01 09:00:00"
#: is the Unix epoch in a UTC+9 camera, 1904 is the classic Mac epoch, 1980 DOS's.
JUNK_DAYS: Final[frozenset[str]] = CLOCK_RESET_DAYS

#: How far ahead of this computer's clock a date may be before it is "future":
#: a photo taken today east of here is already tomorrow by its own clock.
FUTURE_SLACK: Final = timedelta(days=1)


def is_junk_timestamp(normalized: str) -> bool:
    return normalized in JUNK_TIMESTAMPS or normalized[:10] in JUNK_DAYS


EXIFTOOL_FORMAT: Final = "%Y:%m:%d %H:%M:%S"


def format_exif(dt: datetime) -> str:
    return dt.strftime(EXIFTOOL_FORMAT)


def parse_exif(value: str | None) -> datetime | None:
    normalized = normalize_timestamp(value)
    if not normalized:
        return None
    try:
        return datetime.strptime(normalized, EXIFTOOL_FORMAT)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Assessment:
    """Everything classify decided about one file."""

    date_status: DateStatus
    existing_date: str
    proposed_date: str
    date_source: str
    date_match: str
    date_rule: str
    confidence: str
    date_flags: str
    cluster_size: str
    cluster_folders: str
    writable: bool
    inherit_title: str
    inherit_keywords: str
    filename_title: str
    existing_title: str
    existing_keywords: str
    title_to_write: str
    keywords_to_write: str
    apply: bool


def assess_date_status(
    existing: str | None,
    *,
    cluster: Cluster | None,
    now: datetime | None = None,
) -> tuple[DateStatus, list[str]]:
    """Classify the existing date, most severe finding first.

    Precedence is deliberate: a date that is both implausible and clustered is
    reported as implausible, because that is the finding that explains the other
    one. Reporting it as clustered would bury the reason.
    """
    now = now or datetime.now()
    flags: list[str] = []

    normalized = normalize_timestamp(existing)
    if not normalized:
        return "missing", ["no_exif_date"]

    if is_junk_timestamp(normalized):
        return "implausible", ["junk_default"]

    parsed = parse_exif(normalized)
    if parsed is None:
        return "implausible", ["unparseable"]
    if parsed.year < MIN_PLAUSIBLE_YEAR:
        return "implausible", ["pre_1900"]
    if parsed > now + FUTURE_SLACK:
        return "implausible", ["future"]

    if cluster is not None and cluster.size >= CLUSTER_THRESHOLD:
        flags.append("cluster")
        if cluster.folders == 1:
            # One folder, one timestamp, many files is what a camera burst looks
            # like. Flagged so the reviewer can wave it through rather than
            # having it presented as damage.
            flags.append("burst_suspect")
        return "clustered", flags

    return "ok", flags


@dataclass(frozen=True, slots=True)
class Proposal:
    dt: datetime | None
    source: DateSource | None
    match: str
    rule: str
    confidence: Confidence | None


def propose_date(
    stored: StoredFile,
    *,
    folder_chain: list[tuple[str, FolderProposal]],
    clusters: dict[str, Cluster] | None = None,
    now: datetime | None = None,
) -> Proposal:
    """Find the best replacement date, most trustworthy source first.

    1. Another date the photo already carries (XMP or IPTC) when the EXIF one is
       bad: a scanner default in EXIF must not hide a real date in IPTC.
    2. The filename, written by the device or by a human who knew the date
       ("Christmas 1975.jpg"). Two exceptions: an epoch is a save or download
       time, so it loses to a folder date it disagrees with; and a name that
       gives only a year or month gives way to a folder or CreateDate inside it.
    3. The nearest folder with a date.
    4. EXIF:CreateDate, only when it agrees with the folder or nothing else
       offers a date. On a scanned print it is the day of the scan.

    Filesystem created/modified times are never used: they usually record when
    the file was last copied between disks, not when the photo was taken.
    """
    now = now or datetime.now()
    existing = normalize_timestamp(stored.datetime_original)

    def usable(value: str | None) -> datetime | None:
        """A real date, and not the one being replaced or a bulk-edit stamp."""
        dt, norm = parse_exif(value), normalize_timestamp(value)
        if dt is None or norm is None or dt.year < MIN_PLAUSIBLE_YEAR or dt > now:
            return None
        if is_junk_timestamp(norm) or norm == existing or norm in (clusters or {}):
            return None
        return dt

    for tag, value in stored.other_dates:
        if (dt := usable(value)) is not None:
            return Proposal(dt, "existing_metadata", value, tag, "high")

    folder, window, span = _from_folders(folder_chain, now=now)
    create = usable(stored.create_date)

    from_name: DateCandidate | None = parse_filename_date(stored.filename, now=now) or parse_written_date(
        stored.filename, descriptive=bool(derive_file_title(stored.filename)), now=now
    )
    if from_name is not None and from_name.rule in EPOCH_RULES:
        if window is None or window[0] <= from_name.dt < window[1]:
            return Proposal(from_name.dt, "filename", from_name.match, from_name.rule, "medium")
    elif from_name is not None and from_name.rule in COARSE_FOLDER_RULES:
        start, end = date_window(from_name.dt, from_name.rule)
        narrower = window is not None and window[1] - window[0] < end - start
        if folder is not None and folder.dt is not None and narrower and start <= folder.dt < end:
            return folder
        if create is not None and start <= create < end:
            return Proposal(create, "exif_createdate", stored.create_date or "", "exif_createdate", "high")
        return Proposal(from_name.dt, "filename", from_name.match, from_name.rule, "medium")
    elif from_name is not None:
        return Proposal(from_name.dt, "filename", from_name.match, from_name.rule, "high")

    if create is not None and (window is None or window[0] <= create < window[1]):
        # Inside the folder's year, month, or day it is the same date, only
        # more precise. With no folder date it is all there is.
        confidence: Confidence = "high" if window else "medium"
        return Proposal(create, "exif_createdate", stored.create_date or "", "exif_createdate", confidence)
    if folder is not None:
        return folder
    if span is not None:
        return Proposal(None, None, f"{span[0]}-{span[1]}", "multi_year", None)
    return Proposal(None, None, "", "", None)


def _from_folders(
    folder_chain: list[tuple[str, FolderProposal]], *, now: datetime
) -> tuple[Proposal | None, tuple[datetime, datetime] | None, tuple[int, int] | None]:
    """Nearest ancestor folder carrying a date, with the span it stands for.

    A month and day with no year ("2021/12.21 Barney", or "Christmas/2021")
    takes the year from the nearest folder that has one, rather than settling
    for January 1st, as long as the day falls inside that folder's own span:
    "2019-07/Christmas in July" stays July. A folder spanning years
    ("Grandpa (1920-1995)") ends the search: any year above it would be a guess.
    """
    pending: PartialDate | None = None
    for index, (_folder_path, proposal) in enumerate(folder_chain):
        if proposal.year_span is not None:
            first, last = proposal.year_span
            return None, (datetime(first, 1, 1), datetime(last + 1, 1, 1)), proposal.year_span
        if proposal.date is not None:
            partial = pending
            if partial is None and index == 0 and proposal.date_rule == "bare_year" and len(folder_chain) > 1:
                # A photo directly in "Christmas/2021". Not "Christmas Cards/2019/
                # Hawaii": a holiday further up is not stamped onto other trips.
                partial = folder_chain[1][1].partial
            dt = partial.on(proposal.date.year, now=now) if partial is not None else None
            start, end = date_window(proposal.date, proposal.date_rule)
            if partial is not None and dt is not None and start <= dt < end:
                match = f"{partial.match} + {proposal.date.year}"
                rule = f"{partial.rule}+folder_year"
                return Proposal(dt, "folder", match, rule, "medium"), date_window(dt, rule), None
            found = Proposal(proposal.date, "folder", proposal.date_match, proposal.date_rule, "medium")
            return found, date_window(proposal.date, proposal.date_rule), None
        if pending is None:
            pending = proposal.partial
    return None, None, None


#: A camera's photos this far from the exact day their folder names point to a
#: wrong clock, not an event that ran long or a folder named a day late.
CLOCK_SHIFT_MIN_DAYS: Final = 30
#: ...provided they were taken close together, as one event's photos are.
CLOCK_SHIFT_MAX_SPREAD: Final = timedelta(days=3)
CLOCK_SHIFT_MIN_PHOTOS: Final = 3


def exact_folder_day(folder_chain: list[tuple[str, FolderProposal]], *, now: datetime) -> datetime | None:
    """The day the nearest dated folder names, unless a folder above contradicts it.

    "2018/04-20-2019 Photo Shoot" is a typo in one of the two, so neither is
    trusted enough to move photos by.
    """
    proposal, window, _ = _from_folders(folder_chain, now=now)
    if proposal is None or window is None or window[1] - window[0] != timedelta(days=1):
        return None
    day = window[0]
    for _path, folder in folder_chain:
        if folder.year_span is not None and not folder.year_span[0] <= day.year <= folder.year_span[1]:
            return None
        if folder.date is not None:
            start, end = date_window(folder.date, folder.date_rule)
            if not start <= day < end:
                return None
    return day


@dataclass(frozen=True, slots=True)
class ClockSample:
    """A photo with a believable date, in a folder that names an exact day."""

    key: str
    group: tuple[str, str]  # folder, camera
    taken: datetime
    folder_day: datetime


def clock_shifts(samples: list[ClockSample]) -> dict[str, int]:
    """Whole days to add to each photo whose camera clock was plainly wrong.

    Judged per camera per folder: every one of its photos far from the
    folder's day, all taken within a few days of each other. Whole days keep
    the photos' order and time of day.
    """
    groups: dict[tuple[str, str], list[ClockSample]] = {}
    for sample in samples:
        groups.setdefault(sample.group, []).append(sample)
    shifts: dict[str, int] = {}
    for items in groups.values():
        if len(items) < CLOCK_SHIFT_MIN_PHOTOS:
            continue
        day = items[0].folder_day
        near = (day - timedelta(days=CLOCK_SHIFT_MIN_DAYS), day + timedelta(days=CLOCK_SHIFT_MIN_DAYS + 1))
        if any(near[0] <= s.taken < near[1] for s in items):
            continue
        taken = sorted(s.taken for s in items)
        if taken[-1] - taken[0] > CLOCK_SHIFT_MAX_SPREAD:
            continue
        days = (day.date() - taken[len(taken) // 2].date()).days
        shifts.update((s.key, days) for s in items)
    return shifts


def nearest_text(folder_chain: list[tuple[str, FolderProposal]]) -> FolderProposal | None:
    """Nearest ancestor folder that offers a title, innermost first."""
    for _path, proposal in folder_chain:
        if proposal.has_text:
            return proposal
    return None


def decide_apply(
    *,
    date_status: DateStatus,
    proposal: Proposal,
    writable: bool,
    inherit_title: str,
    inherit_keywords: str,
    existing_title: str,
    existing_keywords: str,
    burst_suspect: bool = False,
    loses_precision: bool = False,
    needs_review: bool = False,
) -> bool:
    """The apply-default truth table.

    Defaults aim to be worth reviewing rather than merely safe: a default of 0
    everywhere would be safe and would also make the reviewer hand-edit tens of
    thousands of rows, which is its own kind of failure.
    """
    # Nothing can be stored, so nothing is proposed regardless of merit.
    if not writable:
        return False

    # Derived text is worth writing on its own wherever it fills a gap. Apply
    # never replaces an existing title or keywords, so only gaps count.
    title_fills_gap = bool(inherit_title) and is_placeholder_title(existing_title)
    keywords_fill_gap = bool(inherit_keywords) and not existing_keywords
    text_is_safe = title_fills_gap or keywords_fill_gap

    if date_status == "ok":
        # The date is fine and proposed_date is blank, so an approved row writes
        # text and nothing else.
        return text_is_safe

    if proposal.dt is not None:
        # A one-folder cluster is most likely a camera burst whose timestamp is
        # right. Replacing it (often with a folder's January 1st) must be a
        # deliberate choice, and approving the row would also write the date.
        # So must swapping a clustered but plausible time for a coarser date
        # that agrees with it: that only throws away the time of day. And so
        # must anything `needs_review` names (see assess).
        return not burst_suspect and not loses_precision and not needs_review

    # No candidate date at all, so proposed_date is blank and an approved row is
    # text-only. Same safe case as `ok` above.
    return text_is_safe


def assess(
    stored: StoredFile,
    *,
    folder_chain: list[tuple[str, FolderProposal]],
    clusters: dict[str, Cluster],
    now: datetime | None = None,
    xmp_enabled: bool = True,
) -> Assessment:
    """Full per-file judgment, ready to become a files.csv row."""
    now = now or datetime.now()

    normalized = normalize_timestamp(stored.datetime_original)
    cluster = clusters.get(normalized) if normalized else None
    date_status, flags = assess_date_status(stored.datetime_original, cluster=cluster, now=now)

    proposal = propose_date(stored, folder_chain=folder_chain, clusters=clusters, now=now)
    if stored.error:
        # The metadata was never actually read, so "no date, no keywords" is
        # not known to be true. Proposing anything could replace real values.
        proposal = Proposal(None, None, "", "", None)
    elif date_status != "ok" and proposal.dt is not None and format_exif(proposal.dt) == normalized:
        # Proposing the date the photo already has fixes nothing. It happens on
        # a rescan after apply: forty photos given a folder's 2020-12-25 noon
        # are clustered, and the folder proposes 2020-12-25 noon again.
        proposal = Proposal(None, None, "", "", None)
        flags.append("proposal_matches_existing")
    writable = is_writable(Path(stored.filename), xmp_enabled=xmp_enabled)

    if date_status == "ok":
        # Never propose replacing a date we just judged fine. A blank cell means
        # "leave it", which is both the correct instruction to exiftool and the
        # honest thing to show a reviewer.
        proposed_date = ""
        date_source = ""
        date_match = ""
        date_rule = ""
        confidence = ""
    else:
        proposed_date = format_exif(proposal.dt) if proposal.dt else ""
        date_source = proposal.source or ""
        date_match = proposal.match
        date_rule = proposal.rule
        confidence = proposal.confidence or ""
        if not proposed_date and "proposal_matches_existing" not in flags and not stored.error:
            flags.append("no_candidate")
        if proposal.rule == "bare_year":
            # A folder named "2019" yields 2019-01-01, which renders in Apple
            # Photos as a specific day. Flagged so the reviewer knows the
            # precision is a year, not a date.
            flags.append("year_only")
        elif proposal.rule == "year_month":
            flags.append("month_only")
        elif proposal.rule == "season":
            flags.append("season_only")
        elif proposal.rule == "multi_year":
            flags.append("multi_year")
        if proposal.rule in EPOCH_RULES:
            flags.append("download_time")

    existing_dt = parse_exif(normalized)
    loses_precision = False
    needs_review = False
    if date_status == "clustered" and proposal.dt is not None and existing_dt is not None:
        start, end = date_window(proposal.dt, proposal.rule)
        if proposal.source == "folder" and start <= existing_dt < end:
            loses_precision = True
            flags.append("coarser_than_existing")
        elif proposal.source in ("folder", "exif_createdate"):
            # A plausible date shared across folders may be a deliberate bulk
            # edit ("these scans are all 1985"). A folder's year or a scan day
            # is weaker evidence than that, so replacing it is the reviewer's call.
            needs_review = True
            flags.append("clustered_review")
    if date_status != "ok" and proposal.dt is not None and "ambiguous" in proposal.rule:
        # "04.07.2003": April 7th in the US, 4 July nearly everywhere else.
        needs_review = True
        flags.append("ambiguous_day_month")
    if date_status == "ok" and existing_dt is not None:
        _, window, _ = _from_folders(folder_chain, now=now)
        if window is not None and not window[0] <= existing_dt < window[1]:
            # Review only: a camera clock left at 2001 in "Hawaii 2014", or a
            # scan day in "1975". Nothing is proposed, because the photo's own
            # date usually beats a folder name.
            flags.append("folder_disagrees")

    text = nearest_text(folder_chain)
    inherit_title = text.title if text else ""
    inherit_keywords = text.keywords if text else ""
    filename_title = derive_file_title(stored.filename)

    if not writable:
        flags.append("unwritable")
    if stored.warning:
        flags.append("exiftool_warning")
    if stored.error:
        flags.append("exiftool_error")
    if _mime_mismatch(stored):
        flags.append("mime_mismatch")

    existing_title = stored.title or ""
    existing_keywords = stored.keywords or ""

    should_apply = not stored.error and decide_apply(
        date_status=date_status,
        proposal=proposal,
        writable=writable,
        inherit_title=filename_title or inherit_title,
        inherit_keywords=inherit_keywords,
        existing_title=existing_title,
        existing_keywords=existing_keywords,
        burst_suspect="burst_suspect" in flags,
        loses_precision=loses_precision,
        needs_review=needs_review,
    )

    return Assessment(
        date_status=date_status,
        existing_date=stored.datetime_original or "",
        proposed_date=proposed_date,
        date_source=date_source,
        date_match=date_match,
        date_rule=date_rule,
        confidence=confidence,
        date_flags=",".join(dict.fromkeys(flags)),
        cluster_size=str(cluster.size) if cluster else "",
        cluster_folders=str(cluster.folders) if cluster else "",
        writable=writable,
        inherit_title=inherit_title,
        inherit_keywords=inherit_keywords,
        filename_title=filename_title,
        existing_title=existing_title,
        existing_keywords=existing_keywords,
        title_to_write=(filename_title or inherit_title) if is_placeholder_title(existing_title) else "",
        keywords_to_write="" if existing_keywords else inherit_keywords,
        apply=should_apply,
    )


#: Extensions whose real MIME type is not derivable from the name alone.
_MIME_BY_EXT: Final[dict[str, str]] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


def _mime_mismatch(stored: StoredFile) -> bool:
    """Whether the extension lies about the content.

    A .jpg that is really a PNG is common in archives assembled by hand, and it
    matters here because write capability is keyed on the *real* format: writing
    EXIF to something named .jpg that is actually a .gif will not do what the
    capability table promised.
    """
    expected = _MIME_BY_EXT.get(stored.ext.lower())
    actual = (stored.mime_type or "").lower()
    if not expected or not actual:
        return False
    if expected == actual:
        return False
    # TIFF-based RAW legitimately reports image/tiff, and JPEG has two spellings.
    return not (expected == "image/jpeg" and actual in {"image/jpg", "image/jpeg"})


def build_folder_chain(
    folder: str,
    root: str,
    cache: dict[str, FolderProposal],
) -> list[tuple[str, FolderProposal]]:
    """Ancestor folders from innermost outward, stopping at (and excluding) root.

    Root is excluded because its name describes the archive, not any photo in
    it — inheriting "Photos Backup 2024" onto all 50,000 files would be noise on
    every single one.
    """
    chain: list[tuple[str, FolderProposal]] = []
    current = Path(folder)
    root_path = Path(root)
    while True:
        if current == root_path or current.parent == current:
            break
        proposal = cache.get(str(current))
        if proposal is not None:
            chain.append((str(current), proposal))
        if root_path not in current.parents:
            break
        current = current.parent
    return chain
