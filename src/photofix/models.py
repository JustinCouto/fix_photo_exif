"""Frozen dataclasses for every row structure that crosses a phase boundary.

Dataclasses rather than dicts so a column rename is a type error at import time
instead of a silent KeyError on row 40,000 of a real archive.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .datenames import PartialDate

DateSource = Literal["existing_metadata", "filename", "folder", "exif_createdate", "clock_shift"]
Confidence = Literal["high", "medium"]
DateStatus = Literal["ok", "missing", "implausible", "clustered"]
ConflictClass = Literal["already_applied", "modified_elsewhere", "unwritable"]
VerifyBucket = Literal["verified", "not_applied", "unexpected_change"]


@dataclass(frozen=True, slots=True)
class DateCandidate:
    """A date parsed out of a filename or folder name."""

    dt: datetime
    match: str  # exact matched substring -> the date_match column
    rule: str  # which rule fired -> the date_rule column
    has_time: bool


@dataclass(frozen=True, slots=True)
class FolderProposal:
    """What `folders.classify_folder` decided about one folder name."""

    title: str
    keywords: str  # semicolon-joined
    date: datetime | None
    date_match: str
    date_rule: str
    skip: bool
    #: A month and day with no year in this folder's name ("12.21 Barney"),
    #: waiting for a parent folder to supply the year. None once resolved.
    partial: PartialDate | None = None
    #: Earliest and latest year when the name spans several ("Grandpa
    #: (1920-1995)"). Such a folder gives no date, and stops the search for one.
    year_span: tuple[int, int] | None = None

    @property
    def has_text(self) -> bool:
        """Whether this folder offers anything worth inheriting."""
        return not self.skip and bool(self.title or self.keywords)


@dataclass(frozen=True, slots=True)
class FolderRecord:
    """One row of folders.csv, in the spec's column order."""

    folder_path: str
    file_count: int
    folder_name: str
    title: str
    keywords: str
    #: Information only. Dates are decided per photo in files.csv.
    date_found: str
    skip: int

    @staticmethod
    def columns() -> list[str]:
        return [f.name for f in fields(FolderRecord)]


@dataclass(frozen=True, slots=True)
class FileRecord:
    """One row of files.csv.

    `apply` is held back to the end: it is the only column edited in bulk, so
    it stays at the right-hand edge of the sheet where a fill-down never has to
    skip read-only columns. Editable: `proposed_date`, the two `override_`
    columns, `filename_title` (apply uses it as the title unless it is
    cleared), and `apply`. The rest is review context.
    """

    SourceFile: str
    folder_path: str
    filename: str
    date_status: str
    existing_date: str
    proposed_date: str
    date_source: str
    date_match: str
    confidence: str
    existing_keywords: str
    override_keywords: str
    override_title: str
    scan_size: int
    scan_mtime: str
    # --- added, read-only review context ---
    cluster_size: str
    cluster_folders: str
    date_flags: str
    writable: int
    existing_title: str
    date_rule: str
    inherit_title: str
    inherit_keywords: str
    filename_title: str
    #: What an approved row would write, as of the scan. Edits to folders.csv
    #: or the override columns change it; apply works it out afresh.
    title_to_write: str
    keywords_to_write: str
    # --- the edit target, last by design ---
    apply: int

    @staticmethod
    def columns() -> list[str]:
        return [f.name for f in fields(FileRecord)]


@dataclass(frozen=True, slots=True)
class AppliedRow:
    """One row of applied.csv, before it is serialized to exiftool-native headers.

    Values are already in exiftool's `YYYY:MM:DD HH:MM:SS` form; empty strings
    mean "don't touch", which `-csv=` honors as long as `-f` is never passed.
    """

    source_file: str
    date: str
    keywords: str  # semicolon-joined
    title: str

    @property
    def is_empty(self) -> bool:
        return not (self.date or self.keywords or self.title)


@dataclass(frozen=True, slots=True)
class GuardConflict:
    """A file whose on-disk state no longer matches the scan snapshot."""

    source_file: str
    classification: ConflictClass
    reason: str
    scan_size: int
    actual_size: int
    scan_mtime: float
    actual_mtime: float

    @staticmethod
    def columns() -> list[str]:
        return [
            "SourceFile",
            "classification",
            "reason",
            "scan_size",
            "actual_size",
            "scan_mtime",
            "actual_mtime",
        ]


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """One row of verify_report.csv."""

    source_file: str
    bucket: str
    tag: str
    intended: str
    actual: str
    detail: str

    @staticmethod
    def columns() -> list[str]:
        return ["SourceFile", "bucket", "tag", "intended", "actual", "detail"]


@dataclass(frozen=True, slots=True)
class ScanError:
    """One row of scan_errors.csv."""

    source_file: str
    stage: str
    error: str
    exiftool_stderr: str

    @staticmethod
    def columns() -> list[str]:
        return ["SourceFile", "stage", "error", "exiftool_stderr"]
