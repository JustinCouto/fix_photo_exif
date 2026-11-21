"""The only component that talks to exiftool.

Two invocation shapes, both driven by an argfile so no archive-sized command
line is ever constructed and no shell quoting rules apply:

    read   exiftool -json -G0 <tags> -@ batch.args
    write  exiftool -csv=applied.csv -f -api MissingTagValue=<marker> -sep ";" -@ targets.args

Deliberately absent flags, each for a specific reason:

* No ``-m``. Suppressing minor warnings would hide exactly the "this file is
  subtly broken" signal the audit exists to surface.
* No ``-overwrite_original``. The ``_original`` backups are the undo story.

``-f`` is present, but only together with ``MissingTagValue`` set to
DELETE_MARKER: then a cell holding exactly the marker deletes its tag and an
empty cell is still ignored. Either one alone is dangerous: ``-f`` with the
default makes a cell of "-" a deletion (a title of "-"), and an empty
MissingTagValue makes every blank cell one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from .logging_setup import get_logger

BATCH_SIZE: Final = 500
DEFAULT_TIMEOUT: Final = 300.0
MIN_VERSION: Final = 12.0

#: Requested unqualified; with -G0 they come back group-qualified
#: (EXIF:DateTimeOriginal, File:MIMEType, ...). Warning and Error are requested
#: explicitly because exiftool only includes them in JSON when asked.
READ_TAGS: Final[tuple[str, ...]] = (
    "DateTimeOriginal",
    "CreateDate",
    "ModifyDate",
    "OffsetTimeOriginal",
    "OffsetTimeDigitized",
    "SubSecTimeOriginal",
    "SubSecTimeDigitized",
    "Keywords",
    "ObjectName",
    "Subject",
    "Title",
    "TagsList",
    "HierarchicalSubject",
    "XPTitle",
    "XPKeywords",
    "DateCreated",
    "Make",
    "Model",
    "MIMEType",
    "FileModifyDate",
    "Warning",
    "Error",
    # Every IPTC tag, so apply can tell a file with no IPTC from one whose
    # existing IPTC is in a legacy character set (see apply.iptc_charset).
    "IPTC:All",
)

#: An applied.csv cell holding exactly this deletes the tag (see the module
#: docstring). Chosen so no real title or keyword can collide with it.
DELETE_MARKER: Final = "#photofix-delete#"

#: Added to every record read with READ_TAGS above, so a scan.db read before
#: IPTC:All was requested is recognizable as not knowing a file's IPTC state.
IPTC_READ_MARKER: Final = "Photofix:IPTCRead"

#: Added to every record as well: which READ_TAGS it was read with. A record
#: read before a tag was added cannot say whether the file has that tag.
READ_VERSION_TAG: Final = "Photofix:ReadVersion"
READ_VERSION: Final = hashlib.sha256("\n".join(READ_TAGS).encode()).hexdigest()[:12]

#: List-valued tags. exiftool's JSON gives a bare string for a single value and
#: a list for several, so every consumer would otherwise need this check.
LIST_TAGS: Final[frozenset[str]] = frozenset({"Keywords", "Subject", "TagsList", "HierarchicalSubject"})

TagDict = dict[str, Any]


class ExifToolError(RuntimeError):
    """exiftool could not be run, or failed in a way that is not per-file."""


@dataclass(frozen=True, slots=True)
class BatchFailure:
    """One path exiftool could not read, isolated down to a batch of one."""

    path: str
    reason: str
    stderr: str


@dataclass(frozen=True, slots=True)
class ReadResult:
    records: dict[str, TagDict]
    failures: list[BatchFailure]


class MetadataReader(Protocol):
    """The slice of ExifTool that the guard depends on.

    Narrowing it here keeps `apply.guard` testable without a binary, and makes
    the dependency honest: the guard reads metadata, it does not write.
    """

    def read_many(self, paths: Sequence[Path]) -> ReadResult: ...


def find_exiftool(explicit: str | None = None) -> str:
    """Resolve the binary, with an actionable message when it is missing."""
    candidate = explicit or shutil.which("exiftool")
    if not candidate:
        raise ExifToolError(
            "exiftool not found on PATH. Install it (macOS: `brew install exiftool`, "
            "Windows: `winget install OliverBetz.ExifTool`) and restart the shell — "
            "an already-open terminal keeps a stale PATH."
        )
    if explicit and not Path(explicit).exists():
        raise ExifToolError(f"exiftool not found at the given path: {explicit}")
    return candidate


def normalize_key(path: str | Path) -> str:
    """Canonical lookup key for a path.

    exiftool echoes SourceFile with forward slashes regardless of platform, and
    macOS hands back NFD-normalized filenames where the archive may hold NFC.
    Both are normalized away so a returned record can always be matched to the
    path that was requested.
    """
    text = os.path.normpath(str(path))
    if os.sep == "\\":
        # Only on Windows: elsewhere a backslash is a legal filename character.
        text = text.replace("\\", "/")
    return unicodedata.normalize("NFC", text)


_CSTR_ESCAPES: Final = str.maketrans({"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"})


def argfile_line(path: str) -> str:
    """A path as one argfile line.

    A newline in a filename would otherwise split it into two arguments, and the
    second could be read as an option ("x\\n-Title=y.jpg" writes a title).
    exiftool's `#[CSTR]` prefix marks a line as C-escaped.
    """
    if any(c in path for c in "\n\r"):
        return "#[CSTR]" + path.translate(_CSTR_ESCAPES)
    return path


def normalize_tags(record: TagDict) -> TagDict:
    """Force list-valued tags to always be lists, whatever the arity was."""
    out: TagDict = {}
    for key, value in record.items():
        _, _, bare = key.rpartition(":")
        if bare in LIST_TAGS:
            if value is None:
                out[key] = []
            elif isinstance(value, list):
                out[key] = [str(v) for v in value]
            else:
                out[key] = [str(value)]
        else:
            out[key] = value
    return out


class ExifTool:
    """Batched reader and CSV-driven writer."""

    def __init__(
        self,
        binary: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        batch_size: int = BATCH_SIZE,
        work_dir: Path | None = None,
    ) -> None:
        self.binary = find_exiftool(binary)
        self.timeout = timeout
        self.batch_size = batch_size
        self.work_dir = work_dir
        self.log = get_logger()

    # -- plumbing ---------------------------------------------------------

    def _run(self, args: Sequence[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                [self.binary, *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExifToolError(f"exiftool timed out after {exc.timeout}s") from exc

    def version(self) -> str:
        return self._run(["-ver"], timeout=30).stdout.strip()

    def require_version(self) -> str:
        raw = self.version()
        try:
            if float(raw) < MIN_VERSION:
                raise ExifToolError(f"exiftool {raw} is older than the required {MIN_VERSION}")
        except ValueError as exc:
            raise ExifToolError(f"could not parse exiftool version {raw!r}") from exc
        return raw

    def _write_argfile(self, paths: Iterable[Path | str], name: str) -> Path:
        """One path per line, UTF-8, LF.

        exiftool reads argfiles as UTF-8 and treats each line as a literal
        argument, so paths with spaces, quotes, or accents need no escaping.
        """
        directory = self.work_dir or Path(os.environ.get("TEMP", ".")).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        argfile = directory / name
        with argfile.open("w", encoding="utf-8", newline="\n") as fh:
            for path in paths:
                fh.write(f"{argfile_line(str(path))}\n")
        return argfile

    # -- reading ----------------------------------------------------------

    def _read_once(self, paths: Sequence[Path]) -> tuple[list[TagDict], subprocess.CompletedProcess[str]]:
        argfile = self._write_argfile(paths, "photofix_read.args")
        args = [
            "-json",
            "-G0",
            "-charset",
            "filename=utf8",
            "-api",
            "largefilesupport=1",
            "-FileSize#",  # '#' -> raw bytes, not "1.2 MB"
            *(f"-{tag}" for tag in READ_TAGS),
            "-@",
            str(argfile),
        ]
        proc = self._run(args)
        text = proc.stdout.strip()
        if not text:
            return [], proc
        try:
            # Numbers stay text: a title "1.50" or keyword "007" would otherwise
            # come back as 1.5 and 7, and never match what was written.
            parsed = json.loads(text, parse_float=str, parse_int=str)
        except json.JSONDecodeError:
            return [], proc
        return [dict(item) for item in parsed], proc

    def read_many(self, paths: Sequence[Path]) -> ReadResult:
        """Read metadata for many paths, isolating any that fail.

        exiftool does not stop on a bad file, so a batch usually returns fewer
        records than it was given rather than failing outright. Anything missing
        from the response is re-read via bisection until each failure is pinned
        to a single path — one corrupt file in a 500-file batch costs about nine
        extra invocations instead of 500.
        """
        records: dict[str, TagDict] = {}
        failures: list[BatchFailure] = []
        for start in range(0, len(paths), self.batch_size):
            chunk = paths[start : start + self.batch_size]
            self._read_chunk(chunk, records, failures)
        return ReadResult(records=records, failures=failures)

    def _read_chunk(
        self,
        paths: Sequence[Path],
        records: dict[str, TagDict],
        failures: list[BatchFailure],
    ) -> None:
        if not paths:
            return

        try:
            parsed, proc = self._read_once(paths)
        except ExifToolError as exc:
            # A timeout is attributable to the batch, not to any one file, so it
            # is bisected the same way a parse failure is.
            if len(paths) == 1:
                failures.append(BatchFailure(str(paths[0]), "timeout", str(exc)))
                return
            self._bisect(paths, records, failures)
            return

        got: set[str] = set()
        for record in parsed:
            source = record.get("SourceFile")
            if not source:
                continue
            key = normalize_key(source)
            got.add(key)
            self._keep(source, record, records, failures)

        missing = [p for p in paths if normalize_key(p) not in got]
        if not missing:
            return

        if len(paths) == 1:
            failures.append(
                BatchFailure(
                    str(paths[0]),
                    "exiftool returned no record",
                    (proc.stderr or proc.stdout).strip()[:500],
                )
            )
            return

        # Bisect the whole chunk rather than only `missing`: if exiftool bailed
        # partway through, records after the bad file are absent but fine, and
        # re-reading only the absent ones is exactly the right retry anyway.
        self._bisect(missing, records, failures)

    def _bisect(
        self,
        paths: Sequence[Path],
        records: dict[str, TagDict],
        failures: list[BatchFailure],
    ) -> None:
        if len(paths) == 1:
            parsed: list[TagDict] = []
            stderr = ""
            try:
                parsed, proc = self._read_once(paths)
                stderr = (proc.stderr or "").strip()[:500]
            except ExifToolError as exc:
                stderr = str(exc)
            if parsed:
                self._keep(str(parsed[0].get("SourceFile", paths[0])), parsed[0], records, failures)
            else:
                self.log.warning("unreadable file", extra={"path": str(paths[0]), "stderr": stderr})
                failures.append(BatchFailure(str(paths[0]), "unreadable", stderr))
            return

        mid = len(paths) // 2
        self._read_chunk(paths[:mid], records, failures)
        self._read_chunk(paths[mid:], records, failures)

    def _keep(
        self, source: str, record: TagDict, records: dict[str, TagDict], failures: list[BatchFailure]
    ) -> None:
        """Store a record, or fail it when exiftool could not actually read the file.

        A file it cannot open ("Error opening file") still comes back as a record
        holding only SourceFile and the error. Stored as-is it would look like a
        photo with no date and no keywords, and its real metadata would be
        proposed for replacement.
        """
        error = record.get("ExifTool:Error")
        if error:
            self.log.warning("unreadable file", extra={"path": source, "error": str(error)})
            failures.append(BatchFailure(source, "unreadable", str(error)))
            return
        record[IPTC_READ_MARKER] = True
        record[READ_VERSION_TAG] = READ_VERSION
        records[normalize_key(source)] = normalize_tags(record)

    # -- writing ----------------------------------------------------------

    def apply_csv(
        self,
        csv_path: Path,
        targets: Sequence[Path | str],
        *,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Apply one CSV to a set of files in a single invocation.

        `targets` must be non-empty. With `-csv=` and no file arguments exiftool
        has nothing to operate on and exits having done nothing, which looks
        identical to success — the caller is expected to have already asserted a
        non-empty approved set, and this re-asserts it at the last possible
        moment.
        """
        if not targets:
            raise ExifToolError(
                "refusing to run with an empty target list: exiftool would exit 0 "
                "having written nothing, which is indistinguishable from success"
            )
        argfile = self._write_argfile(targets, "photofix_targets.args")
        args = [
            # Always replace: --merge-keywords writes the deduplicated union,
            # computed by apply, rather than letting -csv+= append duplicates.
            f"-csv={csv_path}",
            "-f",
            "-api",
            f"MissingTagValue={DELETE_MARKER}",
            "-sep",
            ";",
            "-charset",
            "filename=utf8",
            # No global -codedcharacterset=utf8: it relabels a file's existing
            # Latin-1 IPTC as UTF-8 without converting it ("Café" -> "Caf?").
            # applied.csv sets IPTC:CodedCharacterSet per row where that is safe.
            "-P",  # preserve filesystem mtime
            "-@",
            str(argfile),
        ]
        assert "-overwrite_original" not in args
        assert args[args.index("-f") + 2] == f"MissingTagValue={DELETE_MARKER}"
        # One invocation writes the whole archive, and a large file rewrite can
        # take a second, so the ceiling grows with the file count.
        return self._run(args, timeout=timeout or max(self.timeout, 1800.0, 2.0 * len(targets)))


def parse_update_counts(stderr: str) -> dict[str, int]:
    """Pull exiftool's "N image files updated" summary out of its output."""
    counts = {"updated": 0, "unchanged": 0, "errors": 0}
    for line in stderr.splitlines():
        line = line.strip()
        parts = line.split()
        if not parts or not parts[0].isdigit():
            continue
        number = int(parts[0])
        # "N files weren't updated due to errors" also contains "updated".
        if "error" in line.lower():
            counts["errors"] = number
        elif "updated" in line:
            counts["updated"] = number
        elif "unchanged" in line:
            counts["unchanged"] = number
    return counts
