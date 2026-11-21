"""Phase 2 — write the approved changes, and nothing else.

The only component in the tool that modifies an image. Every safety property
lives here:

* Nothing is written without `apply=1` in the reviewed CSV.
* Every approved file is re-stat'd against the scan snapshot first, so a file
  that changed since the scan is never written blind.
* `_original` backups are kept, because they are the undo story.
* Exactly one exiftool invocation writes everything, so a partial run leaves a
  boundary you can reason about rather than an interleaving you cannot.

The guard is load-bearing in a way worth stating plainly: Phase 0 confirmed that
exiftool will happily write a file a second time, leaving the first `_original`
intact but silently overwriting the working copy. Nothing downstream of here
will stop a double apply. This does.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal

from . import EXIT_ATTENTION, EXIT_OK, EXIT_USAGE, xattrs
from .csvio import CsvEncodingError, read_review_csv, write_exiftool_csv, write_review_csv
from .exiftool import (
    DELETE_MARKER,
    IPTC_READ_MARKER,
    READ_VERSION,
    READ_VERSION_TAG,
    ExifTool,
    ExifToolError,
    MetadataReader,
    normalize_key,
    parse_update_counts,
)
from .folders import is_placeholder_title
from .formats import writable_families
from .logging_setup import get_logger, setup_logging
from .models import ConflictClass, FileRecord, FolderRecord, GuardConflict
from .store import ScanStore
from .verify import DATE_TAGS as VERIFY_DATE_TAGS
from .verify import KEYWORD_TAGS as VERIFY_KEYWORD_TAGS
from .verify import TITLE_TAGS as VERIFY_TITLE_TAGS
from .verify import normalize_value

#: applied.csv headers. Group-qualified so each value lands in exactly the group
#: named — verified in Phase 0, where `IPTC:Keywords` did not bleed into XMP and
#: `XMP:Subject` did not bleed into IPTC.
DATE_COLUMNS: Final[dict[str, str]] = {
    "EXIF": "EXIF:DateTimeOriginal",
    "EXIF2": "EXIF:CreateDate",
    "XMP": "XMP:DateCreated",
}
#: Other places the scan finds a capture date, rewritten only where the file
#: already has one: left alone, the old value contradicts the new date, and on a
#: GIF it is the first one the next scan reads. IPTC holds the day only.
EXTRA_DATE_COLUMNS: Final[dict[str, str]] = {
    "XMP:DateTimeOriginal": "XMP",
    "XMP:CreateDate": "XMP",
    "IPTC:DateCreated": "IPTC",
}
#: Deleted with every new date. An old offset or subsecond would otherwise be
#: read as part of it: "+09:00" moves the photo nine hours in Apple Photos.
#: IPTC:TimeCreated goes because exiftool would stamp this computer's offset on
#: a time written without one.
STALE_DATE_COLUMNS: Final[dict[str, str]] = {
    "EXIF:OffsetTimeOriginal": "EXIF",
    "EXIF:OffsetTimeDigitized": "EXIF",
    "EXIF:SubSecTimeOriginal": "EXIF",
    "EXIF:SubSecTimeDigitized": "EXIF",
    "IPTC:TimeCreated": "IPTC",
}
TITLE_COLUMNS: Final[dict[str, str]] = {"IPTC": "IPTC:ObjectName", "XMP": "XMP:Title"}
KEYWORD_COLUMNS: Final[dict[str, str]] = {"IPTC": "IPTC:Keywords", "XMP": "XMP:Subject"}
#: IPTC caps ObjectName and each keyword at 64 bytes.
IPTC_STRING_LIMIT: Final = 64
#: Set per row, only where it cannot relabel existing text (see iptc_charset).
IPTC_CHARSET_COLUMN: Final = "IPTC:CodedCharacterSet"

REQUIRED_FILE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"SourceFile", "apply", "proposed_date", "scan_size", "scan_mtime", "folder_path"}
)

#: Free space is checked against this multiple of the bytes being rewritten,
#: because every written file gains an `_original` copy of itself.
DISK_MARGIN: Final = 1.15

#: How far a spreadsheet's rounding may move scan_mtime before the row is taken
#: to describe a different scan. A whole second, because a sheet that shows the
#: column as an integer saves it that way.
CSV_MTIME_TOLERANCE: Final = 1.0


class ValidationError(Exception):
    """The reviewed CSV cannot be trusted. Nothing is attempted."""


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_ACCEPTED_DATE_FORMATS: Final[tuple[tuple[str, bool], ...]] = (
    ("%Y:%m:%d %H:%M:%S", True),
    ("%Y-%m-%d %H:%M:%S", True),
    ("%Y/%m/%d %H:%M:%S", True),
    ("%Y-%m-%dT%H:%M:%S", True),
    ("%Y:%m:%d %H:%M", False),
    ("%Y-%m-%d %H:%M", False),
    ("%Y:%m:%d", False),
    ("%Y-%m-%d", False),
    ("%Y/%m/%d", False),
)

_SERIAL = re.compile(r"^\d{4,6}(?:\.\d+)?$")
_SLASH_FIRST = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}")


def parse_review_date(text: str) -> str:
    """Accept only year-first dates, and say so loudly otherwise.

    `7/4/2019` is July 4th to Excel in a US locale and April 7th in most others,
    and the CSV carries no record of which machine wrote it. Guessing would put
    a silently wrong capture date on a photo — the exact failure this tool
    exists to repair — so ambiguous formats are rejected rather than
    interpreted. Year-first formats have no such ambiguity and are all accepted.
    """
    raw = text.strip()
    if not raw:
        return ""

    if _SERIAL.match(raw):
        raise ValidationError(
            f"{raw!r} looks like an Excel date serial number, not a date. The column was "
            "reformatted on save; set it to Text and re-enter as YYYY:MM:DD HH:MM:SS."
        )
    if _SLASH_FIRST.match(raw):
        raise ValidationError(
            f"{raw!r} is ambiguous: day-first and month-first locales read it differently, and "
            "the CSV does not record which one wrote it. Re-enter as YYYY:MM:DD HH:MM:SS."
        )

    for fmt, _has_time in _ACCEPTED_DATE_FORMATS:
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        if fmt in ("%Y:%m:%d", "%Y-%m-%d", "%Y/%m/%d"):
            parsed = parsed.replace(hour=12)  # noon, per the scan-side convention
        return parsed.strftime("%Y:%m:%d %H:%M:%S")

    raise ValidationError(f"{raw!r} is not a recognized date. Use YYYY:MM:DD HH:MM:SS.")


def _truthy(text: str) -> bool:
    return text.strip().lower() in {"1", "true", "yes", "y", "x"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FolderRow:
    folder_path: str
    title: str
    keywords: str
    skip: bool


@dataclass(slots=True)
class ApprovedRow:
    source_file: str
    folder_path: str
    date: str
    title: str
    keywords: str
    scan_size: int
    scan_mtime: float
    row_number: int

    @property
    def is_noop(self) -> bool:
        return not (self.date or self.title or self.keywords)


def _column(row: dict[str, str], *names: str) -> str:
    """The first of `names` present: CSVs from before a column rename still load."""
    return next((row[name] for name in names if name in row), "")


def clean_keywords(text: str) -> str:
    """Semicolon-joined, trimmed, without blanks or case-insensitive repeats."""
    kept: dict[str, str] = {}
    for keyword in text.split(";"):
        keyword = keyword.strip()
        if keyword:
            kept.setdefault(keyword.casefold(), keyword)
    return ";".join(kept.values())


def load_folders(path: Path) -> dict[str, FolderRow]:
    if not path.exists():
        raise ValidationError(f"folders.csv not found at {path}")
    rows: dict[str, FolderRow] = {}
    headers_seen = False
    for row in read_review_csv(path):
        headers_seen = True
        missing = {"folder_path", "skip"} - set(row)
        if missing or not {"title", "proposed_title"} & set(row):
            raise ValidationError(
                f"folders.csv is missing columns. Expected the scan's schema: {FolderRecord.columns()}"
            )
        folder = row["folder_path"].strip()
        # Keyed on the canonical form, because resolve_inherited walks ancestors
        # with pathlib and pathlib stringifies with the native separator. A CSV
        # written on one platform and read on another would otherwise never
        # match, and every file would silently inherit nothing.
        rows[normalize_key(folder)] = FolderRow(
            folder_path=folder,
            title=_column(row, "title", "proposed_title").strip(),
            keywords=clean_keywords(_column(row, "keywords", "proposed_keywords")),
            skip=_truthy(row["skip"]),
        )
    if not headers_seen:
        raise ValidationError("folders.csv has no rows")
    return rows


def resolve_inherited(folder_path: str, folders: dict[str, FolderRow]) -> tuple[str, str]:
    """Nearest ancestor folder offering a title, innermost first.

    Resolved here rather than trusting the scan-time `inherit_*` columns, so an
    edit to folders.csv actually takes effect. Those columns are review context;
    folders.csv is the instruction.

    Walked on the canonical forward-slash form, because on macOS `Path` treats
    a backslash as part of a name and a CSV written on Windows would never
    find its parent folders.
    """
    current = PurePosixPath(normalize_key(folder_path))
    while True:
        row = folders.get(normalize_key(str(current)))
        if row is not None and not row.skip and (row.title or row.keywords):
            return row.title, row.keywords
        if current.parent == current:
            return "", ""
        current = current.parent


def load_files(
    path: Path,
    folders: dict[str, FolderRow],
    *,
    store: ScanStore | None = None,
    merge_keywords: bool = False,
) -> tuple[list[ApprovedRow], list[str]]:
    """Read files.csv, keeping only approved rows. Raises on structural problems.

    Whether a photo already has a title or keywords, and whether exiftool could
    read it at all, comes from `store` (scan.db). The CSV's existing_* and
    date_flags columns are context a reviewer can delete or blank, and trusting
    them would let a spreadsheet tidy-up replace real titles. Without a store
    (tests, old callers) the CSV columns are used.
    """
    if not path.exists():
        raise ValidationError(f"files.csv not found at {path}")

    rows = list(enumerate(read_review_csv(path), start=2))  # row 1 is the header
    known = (
        store.get_many([r["SourceFile"].strip() for _, r in rows if r.get("SourceFile", "").strip()])
        if store is not None
        else None
    )

    approved: list[ApprovedRow] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    total = 0

    for index, row in rows:
        total += 1
        missing = REQUIRED_FILE_COLUMNS - set(row)
        if missing:
            raise ValidationError(
                f"files.csv is missing required columns {sorted(missing)}. "
                f"Expected the scan's schema: {FileRecord.columns()}"
            )

        source = row["SourceFile"].strip()
        if not source:
            problems.append(f"row {index}: empty SourceFile")
            continue

        key = normalize_key(source)
        if key in seen:
            # Duplicates usually mean a sort-and-paste accident in the sheet.
            # Applying both would write twice to the same file in one run, which
            # is exactly what the guard exists to prevent.
            problems.append(f"row {index}: duplicate SourceFile, first seen on row {seen[key]}")
            continue
        seen[key] = index

        if not _truthy(row["apply"]):
            continue
        if known is None:
            unreadable = "exiftool_error" in row.get("date_flags", "")
            has_title = not is_placeholder_title(row.get("existing_title", ""))
            has_keywords = bool(row.get("existing_keywords", "").strip())
        else:
            # Not in scan.db: the guard refuses the run. Until then, assume the
            # photo has everything, so nothing is derived over it.
            stored = known.get(key)
            unreadable = stored is not None and bool(stored.error)
            has_title = stored is None or not is_placeholder_title(stored.title or "")
            has_keywords = stored is None or bool(stored.keywords)
            if stored is not None and not unreadable and stored.meta.get(READ_VERSION_TAG) != READ_VERSION:
                problems.append(
                    f"row {index} ({source}): scan.db was made by an older version of photofix that "
                    "did not read every tag apply now checks. Re-run the scan (just scan) first."
                )
                continue
        if unreadable:
            problems.append(
                f"row {index} ({source}): exiftool could not read this file during the scan, so its "
                "existing metadata is unknown. Fix access to it and re-run the scan (just scan), "
                "or set apply=0."
            )
            continue

        try:
            date = parse_review_date(row["proposed_date"])
        except ValidationError as exc:
            problems.append(f"row {index} ({source}): {exc}")
            continue

        # Title: typed override, then a descriptive filename, then the folder.
        # Keywords: typed override, then the folder. A derived value never
        # replaces one the photo already has; only a typed override does.
        # Under --merge-keywords folder keywords are added, so they may go in.
        title = _column(row, "override_title", "proposed_title").strip()
        keywords = clean_keywords(_column(row, "override_keywords", "proposed_keywords"))
        derive_title = not title and not has_title
        derive_keywords = not keywords and (merge_keywords or not has_keywords)
        if derive_title:
            title = row.get("filename_title", "").strip()
        if (derive_title and not title) or derive_keywords:
            folder_title, folder_keywords = resolve_inherited(row["folder_path"].strip(), folders)
            if derive_title and not title:
                title = folder_title
            if derive_keywords:
                keywords = folder_keywords

        try:
            scan_size = int(float(row["scan_size"] or 0))
            scan_mtime = float(row["scan_mtime"] or 0)
        except ValueError:
            problems.append(f"row {index} ({source}): scan_size/scan_mtime are not numeric")
            continue

        approved.append(
            ApprovedRow(
                source_file=source,
                folder_path=row["folder_path"].strip(),
                date=date,
                title=title,
                keywords=keywords,
                scan_size=scan_size,
                scan_mtime=scan_mtime,
                row_number=index,
            )
        )

    get_logger().info("files.csv loaded", extra={"rows": total, "approved": len(approved)})
    return approved, problems


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class GuardOutcome:
    clean: list[ApprovedRow] = field(default_factory=list)
    already_applied: list[GuardConflict] = field(default_factory=list)
    already_applied_rows: list[ApprovedRow] = field(default_factory=list)
    modified_elsewhere: list[GuardConflict] = field(default_factory=list)
    missing: list[GuardConflict] = field(default_factory=list)
    unwritable: list[GuardConflict] = field(default_factory=list)

    @property
    def blocking(self) -> list[GuardConflict]:
        return self.modified_elsewhere + self.missing + self.unwritable


#: exiftool writes `<name>_exiftool_tmp` and keeps `<name>_original`, and most
#: filesystems cap a name at 255 bytes.
_TMP_SUFFIX: Final = "_exiftool_tmp"
_NAME_LIMIT: Final = 255


def write_blocker(path: Path) -> str:
    """Why exiftool would fail to write `path`, or "" if nothing is in the way."""
    if len(path.name.encode("utf-8")) + len(_TMP_SUFFIX) > _NAME_LIMIT:
        return "filename too long for exiftool's temporary copy; shorten it or set apply=0"
    if Path(str(path) + _TMP_SUFFIX).exists():
        return (
            f"a leftover {path.name}{_TMP_SUFFIX} from an interrupted run is in the way; check "
            "the photo opens, then delete that file"
        )
    if not os.access(path, os.W_OK):
        # exiftool would write it anyway (it replaces the file rather than
        # editing it), so a deliberate lock has to be honoured here.
        return "the file is locked or read-only; unlock it or set apply=0"
    return ""


def _matches(meta: dict[str, object], record: dict[str, str]) -> bool:
    """Whether a file carries every value in a rendered applied.csv row.

    Compared against the rendered row, not the reviewed one, so a value that
    could not be stored (no XMP, legacy IPTC) is not looked for.
    """
    return all(
        normalize_value(tag, meta.get(tag)) == normalize_value(tag, value)
        for tag, value in record.items()
        if value and tag not in ("SourceFile", IPTC_CHARSET_COLUMN)
    )


def _unchanged(meta: dict[str, object], baseline: dict[str, object]) -> bool:
    """Whether every tag apply writes still holds its scan-time value."""
    return all(
        normalize_value(tag, meta.get(tag)) == normalize_value(tag, baseline.get(tag))
        for tag in (*VERIFY_DATE_TAGS, *VERIFY_TITLE_TAGS, *VERIFY_KEYWORD_TAGS)
    )


def fit_iptc(text: str, limit: int = IPTC_STRING_LIMIT) -> str:
    """`text` cut to IPTC's byte limit, at a word boundary when one is close.

    exiftool truncates over-long IPTC values itself, mid-word and with a warning
    per file, and the stored value then no longer matches applied.csv. Cutting
    here keeps applied.csv the truth. XMP has no limit and keeps the full text.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    cut = encoded[:limit].decode("utf-8", errors="ignore")
    if not encoded[len(cut.encode("utf-8")) :].startswith(b" ") and " " in cut:
        head = cut.rsplit(" ", 1)[0]
        if len(head) >= limit // 2:
            cut = head
    return cut.rstrip(" ,&+;:-")


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def guard(
    rows: list[ApprovedRow],
    store: ScanStore,
    tool: MetadataReader,
    *,
    xmp_enabled: bool = True,
    merge_keywords: bool = False,
) -> GuardOutcome:
    """Re-stat every approved file against the scan snapshot.

    A size, mtime, or inode that no longer matches means something touched the
    file after the scan. Inode matters because exiftool always writes a new
    file, and with `-P` on a whole-second mtime and an equal-length date, size
    and mtime alone can come out identical. A mismatch is not automatically an
    error — the most common cause is this tool's own previous run — so
    mismatches are read back and sorted:

      already_applied     the file already carries what this row intended, so a
                          re-run is a resumption and the row is simply skipped
      clean               nothing apply writes has changed since the scan (a
                          network volume renumbering inodes, say)
      modified_elsewhere  the file changed into something we did not intend,
                          so it is refused and reported

    Without that distinction, re-running after an interruption would either
    abort on every file already done, or write them all a second time.
    """
    log = get_logger()
    outcome = GuardOutcome()
    suspect: list[ApprovedRow] = []

    known = store.get_many([r.source_file for r in rows])
    for row in rows:
        key = normalize_key(row.source_file)
        stored = known.get(key)
        if stored is None:
            raise ValidationError(
                f"{row.source_file} (row {row.row_number}) is not in scan.db. Either the CSV was "
                "edited to add rows, or it came from a different scan. Re-run the scan (just scan)."
            )
        # The CSV copy of the snapshot only has to identify the scan: Excel keeps
        # 15 significant digits, so "1700000000.123456" comes back rounded, or
        # cut to whole seconds when the column is displayed as a number.
        if row.scan_size != stored.size or abs(row.scan_mtime - stored.mtime) > CSV_MTIME_TOLERANCE:
            raise ValidationError(
                f"{row.source_file} (row {row.row_number}) does not match scan.db: the scan has been "
                "re-run since this files.csv was made. Review the new files.csv instead."
            )
        row.scan_size, row.scan_mtime = stored.size, stored.mtime

        path = Path(row.source_file)
        try:
            st = path.stat()
        except OSError as exc:
            outcome.missing.append(
                GuardConflict(
                    source_file=row.source_file,
                    classification="modified_elsewhere",
                    reason=f"cannot stat: {exc}",
                    scan_size=row.scan_size,
                    actual_size=-1,
                    scan_mtime=row.scan_mtime,
                    actual_mtime=-1.0,
                )
            )
            continue

        blocker = write_blocker(path)
        if blocker:
            outcome.unwritable.append(
                GuardConflict(
                    source_file=row.source_file,
                    classification="unwritable",
                    reason=blocker,
                    scan_size=row.scan_size,
                    actual_size=st.st_size,
                    scan_mtime=row.scan_mtime,
                    actual_mtime=st.st_mtime,
                )
            )
            continue

        same_inode = not stored.inode or st.st_ino == stored.inode
        if st.st_size == row.scan_size and abs(st.st_mtime - row.scan_mtime) < 1e-6 and same_inode:
            outcome.clean.append(row)
        else:
            suspect.append(row)

    if not suspect:
        return outcome

    log.info("re-reading files that changed since the scan", extra={"count": len(suspect)})
    result = tool.read_many([Path(r.source_file) for r in suspect])
    for row in suspect:
        key = normalize_key(row.source_file)
        stored = known[key]
        st = Path(row.source_file).stat()
        meta = result.records.get(key, {})
        _, rendered = build_applied_rows(
            [row],
            xmp_enabled=xmp_enabled,
            iptc_charsets={key: iptc_charset(stored.meta)},
            metas={key: stored.meta},
            merge_keywords=merge_keywords,
        )
        if not rendered:
            # Nothing to write, so nothing to protect: it drops out as a no-op.
            outcome.clean.append(row)
            continue
        if meta and _matches(meta, rendered[0]):
            classification: ConflictClass = "already_applied"
            reason = "already carries the intended values; treating this run as a resumption"
        elif meta and st.st_size == stored.size and _unchanged(meta, stored.meta):
            outcome.clean.append(row)
            continue
        else:
            classification = "modified_elsewhere"
            reason = "changed since the scan and does not match the intended values"
        conflict = GuardConflict(
            source_file=row.source_file,
            classification=classification,
            reason=reason,
            scan_size=row.scan_size,
            actual_size=st.st_size,
            scan_mtime=row.scan_mtime,
            actual_mtime=st.st_mtime,
        )
        if classification == "already_applied":
            outcome.already_applied.append(conflict)
            outcome.already_applied_rows.append(row)
        else:
            outcome.modified_elsewhere.append(conflict)

    return outcome


# ---------------------------------------------------------------------------
# applied.csv
# ---------------------------------------------------------------------------


IptcCharset = Literal["none", "utf8", "legacy"]


def iptc_charset(meta: dict[str, object]) -> IptcCharset:
    """How a file's existing IPTC is encoded, from its scan-time metadata.

    "none" and "utf8" can take IPTC:CodedCharacterSet=UTF8. "legacy" cannot:
    setting it would relabel the existing Latin-1 text as UTF-8 without
    converting it. A scan.db read before IPTC:All was requested is treated as
    legacy, which is the choice that cannot damage anything.
    """
    if not meta.get(IPTC_READ_MARKER):
        return "legacy"
    if not any(k.startswith("IPTC:") for k in meta):
        return "none"
    if str(meta.get(IPTC_CHARSET_COLUMN, "")).replace("-", "").upper() == "UTF8":
        return "utf8"
    return "legacy"


def _latin1(text: str) -> bool:
    try:
        text.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return True


def _split(keywords: str) -> list[str]:
    return [k.strip() for k in keywords.split(";") if k.strip()]


def build_applied_rows(
    rows: list[ApprovedRow],
    *,
    xmp_enabled: bool,
    iptc_charsets: dict[str, IptcCharset] | None = None,
    metas: dict[str, dict[str, Any]] | None = None,
    merge_keywords: bool = False,
    dropped: list[str] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Render approved rows into exiftool-native columns, masked by capability.

    Masking uses the Phase 0 matrix: a `.gif` gets only its XMP columns because
    it verifiably rejects EXIF and IPTC. Writing them anyway would work — the
    values land in the groups that accept them — but would emit a warning per
    file per tag, and a run that prints thousands of warnings trains you to stop
    reading them.

    `metas` maps a file to its scan-time metadata. It decides which extra date
    tags need rewriting, and under `merge_keywords` the union of the existing
    IPTC and XMP keywords with the new ones is written to both, so nothing is
    duplicated and applied.csv says exactly what the file ends up with.
    Approved values with nowhere to go are described in `dropped`.
    """
    columns = ["SourceFile", DATE_COLUMNS["EXIF"], DATE_COLUMNS["EXIF2"]]
    if xmp_enabled:
        columns.append(DATE_COLUMNS["XMP"])
    columns += [c for c, family in EXTRA_DATE_COLUMNS.items() if xmp_enabled or family != "XMP"]
    columns += list(STALE_DATE_COLUMNS)
    columns += [TITLE_COLUMNS["IPTC"], KEYWORD_COLUMNS["IPTC"], IPTC_CHARSET_COLUMN]
    if xmp_enabled:
        columns += [TITLE_COLUMNS["XMP"], KEYWORD_COLUMNS["XMP"]]

    out: list[dict[str, str]] = []
    for row in rows:
        key = normalize_key(row.source_file)
        families = writable_families(Path(row.source_file), xmp_enabled=xmp_enabled)
        charset = (iptc_charsets or {}).get(key, "legacy")
        meta = (metas or {}).get(key, {})
        record = dict.fromkeys(columns, "")
        # exiftool matches SourceFile against the paths it is given, and on
        # Windows it uses forward slashes.
        record["SourceFile"] = str(row.source_file).replace("\\", "/") if os.sep == "\\" else row.source_file
        xmp = "XMP" in families and xmp_enabled

        if row.date:
            if "EXIF" in families:
                record[DATE_COLUMNS["EXIF"]] = row.date
                record[DATE_COLUMNS["EXIF2"]] = row.date
            if xmp:
                record[DATE_COLUMNS["XMP"]] = row.date
            for column, family in EXTRA_DATE_COLUMNS.items():
                if column in record and family in families and meta.get(column) not in (None, ""):
                    record[column] = row.date[:10] if family == "IPTC" else row.date
            for column, family in STALE_DATE_COLUMNS.items():
                if family in families and (family == "EXIF" or meta.get(column) not in (None, "")):
                    record[column] = DELETE_MARKER

        keywords = _split(row.keywords)
        if keywords and merge_keywords:
            existing = [
                *_as_list(meta.get(KEYWORD_COLUMNS["IPTC"])),
                *_as_list(meta.get(KEYWORD_COLUMNS["XMP"])),
            ]
            keywords = _split(clean_keywords(";".join([*existing, *keywords])))
        if "IPTC" in families:
            # Legacy IPTC is Latin-1, so a value it cannot hold goes to XMP only,
            # and the old IPTC value is removed rather than left contradicting it.
            if row.title and (charset != "legacy" or _latin1(row.title)):
                record[TITLE_COLUMNS["IPTC"]] = fit_iptc(row.title)
            elif row.title and xmp and meta.get(TITLE_COLUMNS["IPTC"]) not in (None, ""):
                record[TITLE_COLUMNS["IPTC"]] = DELETE_MARKER
            iptc = clean_keywords(
                ";".join(fit_iptc(k) for k in keywords if charset != "legacy" or _latin1(k))
            )
            if iptc:
                record[KEYWORD_COLUMNS["IPTC"]] = iptc
            elif keywords and xmp and meta.get(KEYWORD_COLUMNS["IPTC"]) not in (None, "", []):
                record[KEYWORD_COLUMNS["IPTC"]] = DELETE_MARKER
            if charset != "legacy" and (record[TITLE_COLUMNS["IPTC"]] or record[KEYWORD_COLUMNS["IPTC"]]):
                record[IPTC_CHARSET_COLUMN] = "UTF8"
        if xmp:
            if row.title:
                record[TITLE_COLUMNS["XMP"]] = row.title
            if keywords:
                record[KEYWORD_COLUMNS["XMP"]] = ";".join(keywords)

        if dropped is not None:
            dropped.extend(_dropped(row, record))
        if any(v for k, v in record.items() if k != "SourceFile"):
            out.append(record)

    return columns, out


def _dropped(row: ApprovedRow, record: dict[str, str]) -> list[str]:
    """Approved values that no column of `record` will store."""
    lost: list[str] = []
    if row.date and not any(record.get(c) for c in DATE_COLUMNS.values()):
        lost.append(f"date {row.date}")
    if row.title and not any(record.get(c) for c in TITLE_COLUMNS.values()):
        lost.append(f"title {row.title!r}")
    stored = {k.casefold() for c in KEYWORD_COLUMNS.values() for k in record.get(c, "").split(";")}
    missing = [k for k in _split(row.keywords) if not {k.casefold(), fit_iptc(k).casefold()} & stored]
    if missing:
        lost.append(f"keywords {';'.join(missing)!r}")
    return [f"{row.source_file} (row {row.row_number}): {what} cannot be stored" for what in lost]


def record_applied(path: Path, columns: list[str], rows: list[dict[str, str]]) -> int:
    """Merge rows into applied.csv, one row per file, newest intent winning.

    Cumulative so that verify still checks the files written by an earlier,
    interrupted run after a resumed one writes the rest. exiftool is given this
    whole file but only this run's targets, and ignores rows for other files.
    """
    merged: dict[str, dict[str, str]] = {}
    all_columns = list(columns)
    if path.exists():
        for row in read_review_csv(path):
            merged[normalize_key(row.get("SourceFile", ""))] = row
            all_columns += [c for c in row if c not in all_columns]
    for row in rows:
        merged[normalize_key(row["SourceFile"])] = row
    write_exiftool_csv(path, all_columns, [r for k, r in merged.items() if k and k != "."])
    return len(merged)


def check_disk_space(rows: list[dict[str, str]], log: object) -> tuple[bool, int, int]:
    """Every written file gains an `_original` copy, so the writes need headroom."""
    needed = 0
    for row in rows:
        try:
            needed += Path(row["SourceFile"]).stat().st_size
        except OSError:
            continue
    # The folder, not the path's anchor: on macOS every anchor is "/", which is
    # the boot disk even when the archive is on an external drive.
    target = Path(rows[0]["SourceFile"]).parent if rows else Path(".")
    free = shutil.disk_usage(target).free
    return free >= needed * DISK_MARGIN, needed, free


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apply.py",
        description="Apply reviewed metadata changes. Writes only rows marked apply=1.",
    )
    parser.add_argument("--files-csv", type=Path, default=Path("out/files.csv"))
    parser.add_argument("--folders-csv", type=Path, default=Path("out/folders.csv"))
    parser.add_argument("--scan-db", type=Path, default=None, help="defaults to <out-dir>/scan.db")
    parser.add_argument("--out-dir", type=Path, default=None, help="defaults to files.csv's directory")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and guard, and write applied.dryrun.csv, without invoking exiftool",
    )
    parser.add_argument(
        "--merge-keywords",
        action="store_true",
        help="add folder keywords to existing ones (without duplicates) instead of skipping such files",
    )
    parser.add_argument("--no-xmp", action="store_true", help="write EXIF and IPTC only")
    parser.add_argument("--exiftool", default=None)
    parser.add_argument(
        "--force-conflicts",
        action="store_true",
        help="proceed with clean files even when some are modified_elsewhere",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir: Path = args.out_dir or args.files_csv.parent
    log = setup_logging(out_dir, verbose=args.verbose, quiet=args.quiet)
    db_path: Path = args.scan_db or (out_dir / "scan.db")

    if not db_path.exists():
        log.error(
            "scan.db not found; apply needs the scan snapshot to guard against changed files",
            extra={"expected": str(db_path)},
        )
        return EXIT_USAGE

    try:
        tool = ExifTool(args.exiftool, work_dir=out_dir)
        tool.require_version()
    except ExifToolError as exc:
        log.error("exiftool unavailable", extra={"error": str(exc)})
        return EXIT_USAGE

    try:
        folders = load_folders(args.folders_csv)
        with ScanStore(db_path) as store:
            approved, problems = load_files(
                args.files_csv, folders, store=store, merge_keywords=args.merge_keywords
            )
    except (ValidationError, CsvEncodingError) as exc:
        log.error("validation failed", extra={"error": str(exc)})
        return EXIT_USAGE

    if problems:
        for problem in problems[:50]:
            log.error("csv problem", extra={"detail": problem})
        log.error("refusing to apply: fix the rows above and re-run", extra={"problems": len(problems)})
        return EXIT_USAGE

    if not approved:
        # Exiftool given zero files exits 0 having done nothing, which is
        # indistinguishable from a successful run. Catch it here instead.
        log.error(
            "no rows have apply=1. Set the apply column in files.csv, or this run would "
            "report success while writing nothing."
        )
        return EXIT_USAGE

    xmp_enabled = not args.no_xmp
    with ScanStore(db_path) as store:
        try:
            outcome = guard(
                approved, store, tool, xmp_enabled=xmp_enabled, merge_keywords=args.merge_keywords
            )
        except ValidationError as exc:
            log.error("guard failed", extra={"error": str(exc)})
            return EXIT_USAGE
        snapshots = store.get_many([r.source_file for r in outcome.clean + outcome.already_applied_rows])

    def render(
        rows: list[ApprovedRow], dropped: list[str] | None = None
    ) -> tuple[list[str], list[dict[str, str]]]:
        return build_applied_rows(
            rows,
            xmp_enabled=xmp_enabled,
            iptc_charsets={key: iptc_charset(s.meta) for key, s in snapshots.items()},
            metas={key: s.meta for key, s in snapshots.items()},
            merge_keywords=args.merge_keywords,
            dropped=dropped,
        )

    if outcome.already_applied:
        log.info(
            "skipping files already carrying the intended values (resumed run)",
            extra={"count": len(outcome.already_applied)},
        )
    if outcome.blocking:
        for conflict in outcome.blocking[:20]:
            log.error(
                "file cannot be written as approved",
                extra={"path": conflict.source_file, "reason": conflict.reason},
            )
        log.error(
            "conflicts detected", extra={"count": len(outcome.blocking), "report": "apply_conflicts.csv"}
        )

    _write_conflicts(out_dir, outcome)

    if outcome.blocking and not args.force_conflicts:
        log.error(
            "refusing to apply. Fix the files above (or re-run the scan with just scan if they "
            "changed), or pass --force-conflicts to write only the files that are still clean."
        )
        return EXIT_ATTENTION

    dropped: list[str] = []
    columns, applied_rows = render(outcome.clean, dropped)
    for detail in dropped[:50]:
        log.warning("approved value has nowhere to go in this file format", extra={"detail": detail})
    if dropped:
        log.warning(
            "some approved values will not be written; see the lines above. --no-xmp, GIF/PNG/HEIC "
            "(no IPTC), and older non-UTF-8 IPTC limit what a file can store.",
            extra={"count": len(dropped)},
        )
    if not applied_rows:
        if outcome.already_applied and not outcome.clean:
            # A completed run, re-run. Reporting this as an error would be
            # actively misleading: the natural response to a red "nothing to
            # write" is to go looking for damage, or to try to undo a change
            # that was correct.
            log.info(
                "nothing to do: every approved file already carries the intended values",
                extra={"already_applied": len(outcome.already_applied)},
            )
            if not args.dry_run:
                record_applied(out_dir / "applied.csv", columns, render(outcome.already_applied_rows)[1])
            _print_summary(out_dir, 0, outcome, dry_run=False)
            return EXIT_OK
        log.error("every approved row resolved to a no-op; nothing to write")
        return EXIT_USAGE

    ok, needed, free = check_disk_space(applied_rows, log)
    log.info(
        "disk preflight",
        extra={"bytes_to_rewrite": needed, "free_bytes": free, "margin": DISK_MARGIN},
    )
    if not ok:
        log.error(
            "not enough free space for the _original backups this run would create",
            extra={"needed_bytes": int(needed * DISK_MARGIN), "free_bytes": free},
        )
        return EXIT_USAGE

    if args.dry_run:
        # Never applied.csv: that is the record verify checks real writes against.
        preview = out_dir / "applied.dryrun.csv"
        write_exiftool_csv(preview, columns, applied_rows)
        log.info(
            "dry run: exiftool not invoked; what would be written is in applied.dryrun.csv",
            extra={"would_write": len(applied_rows), "path": str(preview)},
        )
        _print_summary(out_dir, len(applied_rows), outcome, dry_run=True)
        return EXIT_OK

    applied_csv = out_dir / "applied.csv"
    _, resumed = render(outcome.already_applied_rows)
    total = record_applied(applied_csv, columns, [*resumed, *applied_rows])
    log.info(
        "applied.csv updated", extra={"path": str(applied_csv), "this_run": len(applied_rows), "total": total}
    )

    targets = [r["SourceFile"] for r in applied_rows]
    with ScanStore(db_path) as store:
        store.save_baselines(snapshots[normalize_key(t)] for t in targets if normalize_key(t) in snapshots)
    # exiftool replaces each file with a new one, which leaves Finder tags and
    # other extended attributes behind on `_original`.
    attributes = {t: xattrs.read_all(Path(t)) for t in targets}

    started = time.monotonic()
    log.info("invoking exiftool", extra={"files": len(applied_rows), "merge": args.merge_keywords})
    try:
        proc = tool.apply_csv(applied_csv, targets)
    except ExifToolError as exc:
        log.error("exiftool failed", extra={"error": str(exc)})
        return EXIT_ATTENTION
    finally:
        lost = sum(xattrs.restore(Path(t), saved) for t, saved in attributes.items() if saved)
        if lost:
            log.warning(
                "some Finder tags or extended attributes could not be restored", extra={"count": lost}
            )

    counts = parse_update_counts(proc.stderr + "\n" + proc.stdout)
    log.info(
        "exiftool finished",
        extra={
            "returncode": proc.returncode,
            "elapsed_s": round(time.monotonic() - started, 1),
            **counts,
        },
    )
    for line in (proc.stderr or "").splitlines():
        if line.strip().startswith(("Warning", "Error")):
            log.warning("exiftool", extra={"detail": line.strip()})

    _print_summary(out_dir, counts.get("updated", 0), outcome, dry_run=False)

    if proc.returncode != 0 or counts.get("errors"):
        return EXIT_ATTENTION
    if counts.get("updated", 0) + counts.get("unchanged", 0) < len(targets):
        log.error(
            "exiftool updated fewer files than it was given; run just verify to see which",
            extra={"given": len(targets), **counts},
        )
        return EXIT_ATTENTION
    return EXIT_ATTENTION if outcome.blocking else EXIT_OK


def _write_conflicts(out_dir: Path, outcome: GuardOutcome) -> None:
    rows = [
        {
            "SourceFile": c.source_file,
            "classification": c.classification,
            "reason": c.reason,
            "scan_size": c.scan_size,
            "actual_size": c.actual_size,
            "scan_mtime": f"{c.scan_mtime:.6f}",
            "actual_mtime": f"{c.actual_mtime:.6f}",
        }
        for c in outcome.already_applied + outcome.blocking
    ]
    write_review_csv(out_dir / "apply_conflicts.csv", GuardConflict.columns(), rows)


def _print_summary(out_dir: Path, written: int, outcome: GuardOutcome, *, dry_run: bool) -> None:
    line = "-" * 72
    print(line)
    print(f"  {'would write' if dry_run else 'files written':<26}{written:>10,}")
    print(f"  {'already applied (skipped)':<26}{len(outcome.already_applied):>10,}")
    print(f"  {'conflicts refused':<26}{len(outcome.blocking):>10,}")
    print(line)
    if not dry_run:
        print("  Originals preserved alongside each file as <name>_original.")
        print(f"  Next: just verify <archive folder> {out_dir}")
    print(line)


if __name__ == "__main__":
    raise SystemExit(main())
