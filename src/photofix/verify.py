"""Phase 3 — re-read the archive and check that apply did what it said.

Every file lands in exactly one of three buckets:

    verified           carries what applied.csv intended
    not_applied        the intended value is not there
    unexpected_change  something changed that applied.csv never asked for

The third bucket is the one worth having. The first two only tell you whether
the tool did its job; the third tells you whether it did anything *else*, which
is the failure mode nobody catches by spot-checking a few photos.

Comparison is normalized on both sides. Without that, verify reports near-total
failure on a completely successful run: exiftool returns `2019:07:04 12:00:00`
where the CSV said `2019:07:04 12:00:00` but may append subseconds or a
timezone, and it returns keywords as a list in storage order rather than the
semicolon string that was written. Both are the same value written two ways.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import EXIT_ATTENTION, EXIT_OK, EXIT_USAGE
from .csvio import read_review_csv, write_review_csv
from .exiftool import DELETE_MARKER, ExifTool, ExifToolError, normalize_key
from .logging_setup import setup_logging
from .models import VerifyResult
from .store import ScanStore, normalize_timestamp
from .walker import ORIGINAL_SUFFIX

DATE_TAGS = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "XMP:DateCreated",
    "XMP:DateTimeOriginal",
    "XMP:CreateDate",
    "IPTC:DateCreated",
)
TITLE_TAGS = ("IPTC:ObjectName", "XMP:Title")
KEYWORD_TAGS = ("IPTC:Keywords", "XMP:Subject")


def normalize_value(tag: str, value: Any) -> str:
    """Reduce a tag value to something comparable across a write/read round trip.

    The delete marker means "absent", which reads back as nothing.
    """
    if value is None or value == DELETE_MARKER:
        return ""
    if tag in DATE_TAGS:
        return normalize_timestamp(str(value)) or ""
    if tag in KEYWORD_TAGS:
        items = value if isinstance(value, list) else str(value).split(";")
        # Order is not meaningful and case is not reliably preserved by every
        # writer, so both are normalized away before comparing.
        return ";".join(sorted({i.strip().casefold() for i in items if str(i).strip()}))
    return str(value).strip()


@dataclass(slots=True)
class Report:
    verified: list[VerifyResult] = field(default_factory=list)
    not_applied: list[VerifyResult] = field(default_factory=list)
    unexpected: list[VerifyResult] = field(default_factory=list)
    strays: list[str] = field(default_factory=list)

    @property
    def failures(self) -> int:
        return len(self.not_applied) + len(self.unexpected)

    def all_rows(self) -> list[VerifyResult]:
        return self.verified + self.not_applied + self.unexpected


def load_intent(applied_csv: Path) -> dict[str, dict[str, str]]:
    """Read applied.csv back as {normalized path: {tag: intended value}}."""
    intent: dict[str, dict[str, str]] = {}
    for row in read_review_csv(applied_csv):
        source = row.get("SourceFile", "").strip()
        if not source:
            continue
        intent[normalize_key(source)] = {
            tag: value.strip()
            for tag, value in row.items()
            if tag != "SourceFile" and value and value.strip()
        }
    return intent


def compare(
    intent: dict[str, dict[str, str]],
    actual: dict[str, dict[str, Any]],
    baseline: dict[str, dict[str, Any]] | None = None,
) -> Report:
    """Diff intent against what is on disk now.

    `baseline` is the pre-apply metadata from scan.db. It is what makes the
    third bucket possible: without knowing the prior value, a tag that differs
    from intent is just "not applied", and there is no way to notice a tag that
    changed while never appearing in applied.csv at all.
    """
    report = Report()

    for key, wanted in intent.items():
        current = actual.get(key)
        if current is None:
            report.not_applied.append(
                VerifyResult(key, "not_applied", "*", "", "", "file could not be re-read")
            )
            continue

        # Exact, keywords included: under --merge-keywords applied.csv already
        # holds the full union, so anything more or less is not what was asked.
        for tag, intended in wanted.items():
            got = normalize_value(tag, current.get(tag))
            want = normalize_value(tag, intended)
            if got == want:
                report.verified.append(
                    VerifyResult(key, "verified", tag, intended, str(current.get(tag, "")), "")
                )
            else:
                report.not_applied.append(
                    VerifyResult(
                        key,
                        "not_applied",
                        tag,
                        intended,
                        str(current.get(tag, "")),
                        "intended value is not present",
                    )
                )

        if baseline is not None:
            before = baseline.get(key, {})
            for tag in (*DATE_TAGS, *TITLE_TAGS, *KEYWORD_TAGS):
                if tag in wanted:
                    continue  # asked for; handled above
                old = normalize_value(tag, before.get(tag))
                new = normalize_value(tag, current.get(tag))
                if old != new:
                    report.unexpected.append(
                        VerifyResult(
                            key,
                            "unexpected_change",
                            tag,
                            "",
                            str(current.get(tag, "")),
                            f"changed from {before.get(tag, '')!r} but applied.csv never asked",
                        )
                    )

    return report


def find_strays(root: Path) -> list[str]:
    """`_original` backups left in the tree.

    Reported rather than removed. They are the only undo path, and deleting
    them is a decision for a human who has confirmed the results are good.
    """
    return [str(p) for p in root.rglob(f"*{ORIGINAL_SUFFIX}") if p.is_file()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verify.py",
        description="Re-read the archive and diff it against what apply.py intended.",
    )
    parser.add_argument("--applied-csv", type=Path, default=Path("out/applied.csv"))
    parser.add_argument("--root", type=Path, default=None, help="archive root, for _original sweep")
    parser.add_argument("--scan-db", type=Path, default=None, help="defaults to <out-dir>/scan.db")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="check only the files in applied.csv; skip the tree sweep for _original strays",
    )
    parser.add_argument("--exiftool", default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir: Path = args.out_dir or args.applied_csv.parent
    log = setup_logging(out_dir, verbose=args.verbose, quiet=args.quiet)

    if not args.applied_csv.exists():
        log.error("applied.csv not found", extra={"expected": str(args.applied_csv)})
        return EXIT_USAGE

    try:
        tool = ExifTool(args.exiftool, work_dir=out_dir)
        tool.require_version()
    except ExifToolError as exc:
        log.error("exiftool unavailable", extra={"error": str(exc)})
        return EXIT_USAGE

    intent = load_intent(args.applied_csv)
    if not intent:
        log.error("applied.csv has no rows to verify")
        return EXIT_USAGE

    log.info("verifying", extra={"files": len(intent)})
    started = time.monotonic()

    paths = [Path(key) for key in intent]
    result = tool.read_many(paths)
    if result.failures:
        log.warning("some files could not be re-read", extra={"count": len(result.failures)})

    baseline: dict[str, dict[str, Any]] | None = None
    db_path: Path = args.scan_db or (out_dir / "scan.db")
    if db_path.exists():
        with ScanStore(db_path) as store:
            baseline = store.baselines(intent)
        log.info("baseline loaded from scan.db", extra={"files": len(baseline)})
    else:
        log.warning(
            "scan.db not found: unexpected_change detection is disabled, so this run can only "
            "confirm intended writes, not rule out unintended ones",
            extra={"expected": str(db_path)},
        )

    report = compare(intent, result.records, baseline)

    if not args.quick:
        root: Path = args.root or _infer_root(intent)
        if root.is_dir():
            report.strays = find_strays(root)
            log.info("_original backups present", extra={"count": len(report.strays)})

    write_review_csv(
        out_dir / "verify_report.csv",
        VerifyResult.columns(),
        [
            {
                "SourceFile": r.source_file,
                "bucket": r.bucket,
                "tag": r.tag,
                "intended": r.intended,
                "actual": r.actual,
                "detail": r.detail,
            }
            for r in report.all_rows()
        ],
    )
    if report.strays:
        write_review_csv(
            out_dir / "originals.csv",
            ["path"],
            [{"path": p} for p in report.strays],
        )

    log.info(
        "verify complete",
        extra={
            "verified": len(report.verified),
            "not_applied": len(report.not_applied),
            "unexpected_change": len(report.unexpected),
            "originals": len(report.strays),
            "elapsed_s": round(time.monotonic() - started, 1),
        },
    )
    _print_summary(out_dir, report)

    return EXIT_ATTENTION if report.failures else EXIT_OK


def _infer_root(intent: dict[str, dict[str, str]]) -> Path:
    """Deepest directory containing every verified file."""
    parts = [Path(key).parent.parts for key in intent]
    if not parts:
        return Path(".")
    common: list[str] = []
    for chunk in zip(*parts, strict=False):
        if len(set(chunk)) != 1:
            break
        common.append(chunk[0])
    return Path(*common) if common else Path(".")


def _print_summary(out_dir: Path, report: Report) -> None:
    line = "-" * 72
    print(line)
    print(f"  verified           {len(report.verified):>10,}")
    print(f"  not applied        {len(report.not_applied):>10,}")
    print(f"  unexpected changes {len(report.unexpected):>10,}")
    print(f"  _original backups  {len(report.strays):>10,}")
    print(line)
    if report.failures:
        print(f"  Failures listed in {out_dir / 'verify_report.csv'}.")
    else:
        print("  Everything applied.csv asked for is present, and no other date, title, or")
        print("  keyword tag changed. (Other metadata is not compared.)")
    if report.strays:
        print("  _original backups are your undo path; delete them only once you are satisfied.")
    print(line)


if __name__ == "__main__":
    raise SystemExit(main())
