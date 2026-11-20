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
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from . import EXIT_ATTENTION, EXIT_OK, EXIT_USAGE
from .csvio import read_review_csv, write_exiftool_csv, write_review_csv
from .exiftool import ExifTool, ExifToolError, MetadataReader, normalize_key, parse_update_counts
from .formats import writable_families
from .logging_setup import get_logger, setup_logging
from .models import FileRecord, FolderRecord, GuardConflict
from .store import ScanStore, normalize_timestamp

#: applied.csv headers. Group-qualified so each value lands in exactly the group
#: named — verified in Phase 0, where `IPTC:Keywords` did not bleed into XMP and
#: `XMP:Subject` did not bleed into IPTC.
DATE_COLUMNS: Final[dict[str, str]] = {
    "EXIF": "EXIF:DateTimeOriginal",
    "EXIF2": "EXIF:CreateDate",
    "XMP": "XMP:DateCreated",
}
TITLE_COLUMNS: Final[dict[str, str]] = {"IPTC": "IPTC:ObjectName", "XMP": "XMP:Title"}
KEYWORD_COLUMNS: Final[dict[str, str]] = {"IPTC": "IPTC:Keywords", "XMP": "XMP:Subject"}

REQUIRED_FILE_COLUMNS: Final[frozenset[str]] = frozenset(
    {"SourceFile", "apply", "proposed_date", "scan_size", "scan_mtime", "folder_path"}
)

#: Free space is checked against this multiple of the bytes being rewritten,
#: because every written file gains an `_original` copy of itself.
DISK_MARGIN: Final = 1.15


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


def load_folders(path: Path) -> dict[str, FolderRow]:
    if not path.exists():
        raise ValidationError(f"folders.csv not found at {path}")
    rows: dict[str, FolderRow] = {}
    headers_seen = False
    for row in read_review_csv(path):
        headers_seen = True
        missing = set(FolderRecord.columns()) - set(row)
        if missing:
            raise ValidationError(f"folders.csv is missing columns: {sorted(missing)}")
        folder = row["folder_path"].strip()
        # Keyed on the canonical form, because resolve_inherited walks ancestors
        # with pathlib and pathlib stringifies with the native separator. A CSV
        # written on one platform and read on another would otherwise never
        # match, and every file would silently inherit nothing.
        rows[normalize_key(folder)] = FolderRow(
            folder_path=folder,
            title=row["proposed_title"].strip(),
            keywords=row["proposed_keywords"].strip(),
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
    """
    current = Path(folder_path)
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
) -> tuple[list[ApprovedRow], list[str]]:
    """Read files.csv, keeping only approved rows. Raises on structural problems."""
    if not path.exists():
        raise ValidationError(f"files.csv not found at {path}")

    approved: list[ApprovedRow] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    total = 0

    for index, row in enumerate(read_review_csv(path), start=2):  # row 1 is the header
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

        try:
            date = parse_review_date(row["proposed_date"])
        except ValidationError as exc:
            problems.append(f"row {index} ({source}): {exc}")
            continue

        # A file-level override beats folder inheritance; blank falls back.
        title = row.get("proposed_title", "").strip()
        keywords = row.get("proposed_keywords", "").strip()
        if not title and not keywords:
            title, keywords = resolve_inherited(row["folder_path"].strip(), folders)
        elif not title:
            title, _ = resolve_inherited(row["folder_path"].strip(), folders)
        elif not keywords:
            _, keywords = resolve_inherited(row["folder_path"].strip(), folders)

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
    modified_elsewhere: list[GuardConflict] = field(default_factory=list)
    missing: list[GuardConflict] = field(default_factory=list)

    @property
    def blocking(self) -> list[GuardConflict]:
        return self.modified_elsewhere + self.missing


def _matches_intent(meta: dict[str, object], row: ApprovedRow) -> bool:
    """Whether a file already carries what this row intended to write."""
    if row.date:
        current = normalize_timestamp(str(meta.get("EXIF:DateTimeOriginal") or "")) or ""
        if current != normalize_timestamp(row.date):
            return False
    if row.title:
        current_title = str(meta.get("IPTC:ObjectName") or meta.get("XMP:Title") or "")
        if current_title != row.title:
            return False
    if row.keywords:
        have = {k.casefold() for k in _as_list(meta.get("IPTC:Keywords")) + _as_list(meta.get("XMP:Subject"))}
        want = {k.strip().casefold() for k in row.keywords.split(";") if k.strip()}
        if not want.issubset(have):
            return False
    return True


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]


def guard(rows: list[ApprovedRow], store: ScanStore, tool: MetadataReader) -> GuardOutcome:
    """Re-stat every approved file against the scan snapshot.

    A size or mtime that no longer matches means something touched the file
    after the scan. That is not automatically an error — the most common cause
    is this tool's own previous run — so mismatches are read back and sorted:

      already_applied     the file already carries what this row intended, so a
                          re-run is a resumption and the row is simply skipped
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
                "edited to add rows, or it came from a different scan. Re-run scan.py."
            )

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

        if st.st_size == row.scan_size and abs(st.st_mtime - row.scan_mtime) < 1e-6:
            outcome.clean.append(row)
        else:
            suspect.append(row)

    if not suspect:
        return outcome

    log.info("re-reading files that changed since the scan", extra={"count": len(suspect)})
    result = tool.read_many([Path(r.source_file) for r in suspect])
    for row in suspect:
        st = Path(row.source_file).stat()
        meta = result.records.get(normalize_key(row.source_file), {})
        conflict = GuardConflict(
            source_file=row.source_file,
            classification="already_applied" if _matches_intent(meta, row) else "modified_elsewhere",
            reason=(
                "already carries the intended values; treating this run as a resumption"
                if _matches_intent(meta, row)
                else "changed since the scan and does not match the intended values"
            ),
            scan_size=row.scan_size,
            actual_size=st.st_size,
            scan_mtime=row.scan_mtime,
            actual_mtime=st.st_mtime,
        )
        if conflict.classification == "already_applied":
            outcome.already_applied.append(conflict)
        else:
            outcome.modified_elsewhere.append(conflict)

    return outcome


# ---------------------------------------------------------------------------
# applied.csv
# ---------------------------------------------------------------------------


def build_applied_rows(
    rows: list[ApprovedRow], *, xmp_enabled: bool
) -> tuple[list[str], list[dict[str, str]]]:
    """Render approved rows into exiftool-native columns, masked by capability.

    Masking uses the Phase 0 matrix: a `.gif` gets only its XMP columns because
    it verifiably rejects EXIF and IPTC. Writing them anyway would work — the
    values land in the groups that accept them — but would emit a warning per
    file per tag, and a run that prints thousands of warnings trains you to stop
    reading them.
    """
    columns = ["SourceFile", DATE_COLUMNS["EXIF"], DATE_COLUMNS["EXIF2"]]
    if xmp_enabled:
        columns.append(DATE_COLUMNS["XMP"])
    columns += [TITLE_COLUMNS["IPTC"], KEYWORD_COLUMNS["IPTC"]]
    if xmp_enabled:
        columns += [TITLE_COLUMNS["XMP"], KEYWORD_COLUMNS["XMP"]]

    out: list[dict[str, str]] = []
    for row in rows:
        families = writable_families(Path(row.source_file), xmp_enabled=xmp_enabled)
        record = dict.fromkeys(columns, "")
        # exiftool matches SourceFile against the paths it is given, and it uses
        # forward slashes on every platform.
        record["SourceFile"] = str(row.source_file).replace("\\", "/")

        if row.date:
            if "EXIF" in families:
                record[DATE_COLUMNS["EXIF"]] = row.date
                record[DATE_COLUMNS["EXIF2"]] = row.date
            if "XMP" in families and xmp_enabled:
                record[DATE_COLUMNS["XMP"]] = row.date
        if row.title:
            if "IPTC" in families:
                record[TITLE_COLUMNS["IPTC"]] = row.title
            if "XMP" in families and xmp_enabled:
                record[TITLE_COLUMNS["XMP"]] = row.title
        if row.keywords:
            if "IPTC" in families:
                record[KEYWORD_COLUMNS["IPTC"]] = row.keywords
            if "XMP" in families and xmp_enabled:
                record[KEYWORD_COLUMNS["XMP"]] = row.keywords

        if any(v for k, v in record.items() if k != "SourceFile"):
            out.append(record)

    return columns, out


def check_disk_space(rows: list[dict[str, str]], log: object) -> tuple[bool, int, int]:
    """Every written file gains an `_original` copy, so the writes need headroom."""
    needed = 0
    for row in rows:
        try:
            needed += Path(row["SourceFile"]).stat().st_size
        except OSError:
            continue
    target = Path(rows[0]["SourceFile"]).anchor if rows else "."
    free = shutil.disk_usage(target or ".").free
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
        help="validate, guard, and write applied.csv without invoking exiftool",
    )
    parser.add_argument(
        "--merge-keywords",
        action="store_true",
        help="add to existing keywords (-csv+=) instead of replacing them (-csv=)",
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
        approved, problems = load_files(args.files_csv, folders)
    except ValidationError as exc:
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

    with ScanStore(db_path) as store:
        try:
            outcome = guard(approved, store, tool)
        except ValidationError as exc:
            log.error("guard failed", extra={"error": str(exc)})
            return EXIT_USAGE

    if outcome.already_applied:
        log.info(
            "skipping files already carrying the intended values (resumed run)",
            extra={"count": len(outcome.already_applied)},
        )
    if outcome.blocking:
        for conflict in outcome.blocking[:20]:
            log.error(
                "file changed since the scan",
                extra={"path": conflict.source_file, "reason": conflict.reason},
            )
        log.error(
            "conflicts detected", extra={"count": len(outcome.blocking), "report": "apply_conflicts.csv"}
        )

    _write_conflicts(out_dir, outcome)

    if outcome.blocking and not args.force_conflicts:
        log.error(
            "refusing to apply. Re-run scan.py to refresh the snapshot, or pass "
            "--force-conflicts to write only the files that are still clean."
        )
        return EXIT_ATTENTION

    columns, applied_rows = build_applied_rows(outcome.clean, xmp_enabled=not args.no_xmp)
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
            _print_summary(out_dir, 0, outcome, dry_run=False)
            return EXIT_OK
        log.error("every approved row resolved to a no-op; nothing to write")
        return EXIT_USAGE

    applied_csv = out_dir / "applied.csv"
    write_exiftool_csv(applied_csv, columns, applied_rows)
    log.info("applied.csv written", extra={"path": str(applied_csv), "rows": len(applied_rows)})

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
        log.info(
            "dry run: applied.csv written, exiftool not invoked",
            extra={"would_write": len(applied_rows)},
        )
        _print_summary(out_dir, len(applied_rows), outcome, dry_run=True)
        return EXIT_OK

    started = time.monotonic()
    log.info("invoking exiftool", extra={"files": len(applied_rows), "merge": args.merge_keywords})
    try:
        proc = tool.apply_csv(
            applied_csv,
            [r["SourceFile"] for r in applied_rows],
            merge_keywords=args.merge_keywords,
        )
    except ExifToolError as exc:
        log.error("exiftool failed", extra={"error": str(exc)})
        return EXIT_ATTENTION

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
        for c in outcome.already_applied + outcome.modified_elsewhere + outcome.missing
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
        print(f"  Next: verify.py --applied-csv {out_dir / 'applied.csv'}")
    print(line)


if __name__ == "__main__":
    raise SystemExit(main())
