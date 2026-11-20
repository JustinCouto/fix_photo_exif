"""Decide each file's date status and what to propose for it.

Two separable judgments that are easy to conflate:

* `date_status` describes what is *wrong* with the existing date, if anything.
* the proposal describes what we would put there instead, and how much we trust it.

A file can be `ok` and still get a proposal (a folder-derived title), and it can
be `missing` with no proposal at all (nothing in the filename, folder, or
filesystem was usable). The `apply` default has to handle both.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from .datenames import parse_filename_date
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
    existing_title: str
    existing_keywords: str
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

    if normalized in JUNK_TIMESTAMPS:
        return "implausible", ["junk_default"]

    parsed = parse_exif(normalized)
    if parsed is None:
        return "implausible", ["unparseable"]
    if parsed.year < MIN_PLAUSIBLE_YEAR:
        return "implausible", ["pre_1900"]
    if parsed > now:
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
    now: datetime | None = None,
) -> Proposal:
    """Find the best replacement date, most trustworthy source first.

    The priority order is about *provenance*, not precision. A filename date was
    written by the device or by a human who knew when the photo was taken; a
    filesystem timestamp usually records when the file was last copied between
    disks, which is why it is last and never trusted enough to auto-apply.
    """
    now = now or datetime.now()

    # 0. EXIF:CreateDate. Same camera, same moment, written by the same firmware
    #    as DateTimeOriginal — when only one of the two survived an edit, this
    #    is better evidence than anything in the filename.
    create = parse_exif(stored.create_date)
    if (
        create is not None
        and create.year >= MIN_PLAUSIBLE_YEAR
        and create <= now
        and normalize_timestamp(stored.create_date) not in JUNK_TIMESTAMPS
    ):
        return Proposal(create, "exif_createdate", stored.create_date or "", "exif_createdate", "high")

    # 1. Filename.
    from_name: DateCandidate | None = parse_filename_date(stored.filename, now=now)
    if from_name is not None:
        return Proposal(from_name.dt, "filename", from_name.match, from_name.rule, "high")

    # 2. Nearest ancestor folder carrying a date.
    for _folder_path, proposal in folder_chain:
        if proposal.date is not None:
            return Proposal(proposal.date, "folder", proposal.date_match, proposal.date_rule, "medium")

    # 3. Filesystem. min(birthtime, mtime) because a copy updates mtime forward
    #    and, on some transfers, birthtime too — the earlier of the two is the
    #    closer bound on when the file first existed.
    stamps = [t for t in (stored.birthtime, stored.mtime) if t]
    if stamps:
        try:
            fs_dt = datetime.fromtimestamp(min(stamps))
        except (OverflowError, OSError, ValueError):
            fs_dt = None
        if fs_dt is not None and fs_dt.year >= MIN_PLAUSIBLE_YEAR and fs_dt <= now:
            return Proposal(fs_dt, "filesystem", "", "min_birthtime_mtime", "low")

    return Proposal(None, None, "", "", None)


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
) -> bool:
    """The apply-default truth table.

    Defaults aim to be worth reviewing rather than merely safe: a default of 0
    everywhere would be safe and would also make the reviewer hand-edit tens of
    thousands of rows, which is its own kind of failure.
    """
    # Nothing can be stored, so nothing is proposed regardless of merit.
    if not writable:
        return False

    # Folder-derived text is worth writing on its own, but only where there is
    # nothing already there to clobber.
    text_is_safe = bool(inherit_title or inherit_keywords) and not existing_title and not existing_keywords

    if date_status == "ok":
        # The date is fine and proposed_date is blank, so an approved row writes
        # text and nothing else.
        return text_is_safe

    if proposal.dt is not None and proposal.confidence in ("high", "medium"):
        return True

    if proposal.dt is not None:
        # Low confidence, which in practice means a filesystem timestamp — often
        # the date the file was last copied between disks, not when it was taken.
        #
        # `apply` approves the whole row, so returning True here because folder
        # text looked worthwhile would also write the guessed date. That is how
        # a run intended to add the title "Bulk Export" ends up stamping today's
        # date onto every photo in it. The date is still shown for review; it
        # just has to be opted into deliberately.
        return False

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

    proposal = propose_date(stored, folder_chain=folder_chain, now=now)
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
        if not proposed_date:
            flags.append("no_candidate")
        elif proposal.source == "filesystem":
            flags.append("fs_only")
        if proposal.rule == "bare_year":
            # A folder named "2019" yields 2019-01-01, which renders in Apple
            # Photos as a specific day. Flagged so the reviewer knows the
            # precision is a year, not a date.
            flags.append("year_only")

    text = nearest_text(folder_chain)
    inherit_title = text.title if text else ""
    inherit_keywords = text.keywords if text else ""

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

    should_apply = decide_apply(
        date_status=date_status,
        proposal=proposal,
        writable=writable,
        inherit_title=inherit_title,
        inherit_keywords=inherit_keywords,
        existing_title=existing_title,
        existing_keywords=existing_keywords,
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
        existing_title=existing_title,
        existing_keywords=existing_keywords,
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
