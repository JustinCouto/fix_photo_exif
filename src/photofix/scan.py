"""Phase 1 — walk, read, classify, and propose. Never writes to an image.

Three stages, each of which can be interrupted and resumed:

    A  walk the tree and record paths, sizes, and timestamps
    B  read metadata in batches and cache it
    C  classify and emit the review CSVs

Stage C is recomputed from scratch every run rather than cached. It is cheap
relative to B, and it means adjusting a threshold and re-running produces fresh
proposals without re-reading the archive.
"""

from __future__ import annotations

import argparse
import shutil
import time
from datetime import datetime
from pathlib import Path

from . import EXIT_ATTENTION, EXIT_OK, EXIT_USAGE
from .classify import CLUSTER_THRESHOLD, assess, build_folder_chain, format_exif
from .csvio import write_review_csv
from .exiftool import ExifTool, ExifToolError
from .folders import classify_folder
from .logging_setup import ProgressReporter, get_logger, setup_logging
from .models import FileRecord, FolderProposal, FolderRecord, ScanError
from .store import ScanStore
from .walker import WalkStats, walk_images

WALK_CHUNK = 2000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scan.py",
        description="Audit photo metadata and propose fixes. Reads only; never writes to an image.",
    )
    parser.add_argument("--root", required=True, type=Path, help="archive root to walk")
    parser.add_argument("--out-dir", type=Path, default=Path("out"), help="where artifacts go")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="stop after N images (rehearsal). Clustering is unreliable under --limit.",
    )
    parser.add_argument(
        "--fresh", action="store_true", help="discard scan.db and start over rather than resuming"
    )
    parser.add_argument("--exiftool", default=None, help="path to the exiftool binary")
    parser.add_argument("--batch-size", type=int, default=500, help="files per exiftool invocation")
    parser.add_argument(
        "--cluster-threshold",
        type=int,
        default=CLUSTER_THRESHOLD,
        help="how many files must share a timestamp before it is suspect",
    )
    parser.add_argument(
        "--no-xmp", action="store_true", help="evaluate write capability without XMP as a target"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    out_dir: Path = args.out_dir
    log = setup_logging(out_dir, verbose=args.verbose, quiet=args.quiet)

    root: Path = args.root.expanduser()
    if not root.is_dir():
        log.error("root is not a directory", extra={"root": str(root)})
        return EXIT_USAGE
    root = root.resolve()

    db_path = out_dir / "scan.db"
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(db_path) + suffix)
            if candidate.exists():
                candidate.unlink()
        log.info("discarded previous scan state", extra={"db": str(db_path)})

    try:
        tool = ExifTool(args.exiftool, batch_size=args.batch_size, work_dir=out_dir)
        version = tool.require_version()
    except ExifToolError as exc:
        log.error("exiftool unavailable", extra={"error": str(exc)})
        return EXIT_USAGE

    started = time.monotonic()
    log.info("scan starting", extra={"root": str(root), "out_dir": str(out_dir), "exiftool": version})

    with ScanStore(db_path) as store:
        previous_root = store.get_meta("root")
        if previous_root and previous_root != str(root):
            log.error(
                "scan.db belongs to a different root; pass --fresh to start over",
                extra={"existing_root": previous_root, "requested_root": str(root)},
            )
            return EXIT_USAGE
        store.set_meta("root", str(root))

        stats = _stage_a(store, root, limit=args.limit, log=log)
        failures = _stage_b(store, tool, log=log)
        counts = _stage_c(
            store,
            root,
            out_dir,
            failures=failures,
            cluster_threshold=args.cluster_threshold,
            xmp_enabled=not args.no_xmp,
            limited=args.limit is not None,
            log=log,
        )

    elapsed = round(time.monotonic() - started, 1)
    log.info(
        "scan complete",
        extra={"elapsed_s": elapsed, **stats.as_dict(), **counts, "out_dir": str(out_dir)},
    )
    _print_summary(out_dir, stats, counts, failures_count=len(failures))

    return EXIT_ATTENTION if failures else EXIT_OK


def _stage_a(store: ScanStore, root: Path, *, limit: int | None, log: object) -> WalkStats:
    logger = get_logger()
    logger.info("stage A: walking", extra={"root": str(root)})
    stats = WalkStats()
    batch = []
    total = 0
    for walked in walk_images(root, limit=limit, stats=stats):
        batch.append(walked)
        if len(batch) >= WALK_CHUNK:
            total += store.upsert_walked(batch)
            batch.clear()
    if batch:
        total += store.upsert_walked(batch)
    store.mark_walk_complete(limit is None)
    logger.info("stage A complete", extra={"recorded": total, **stats.as_dict()})
    return stats


def _stage_b(store: ScanStore, tool: ExifTool, *, log: object) -> list[ScanError]:
    logger = get_logger()
    pending = store.pending_paths()
    already = store.count() - len(pending)
    if already:
        logger.info("resuming: metadata already cached", extra={"cached": already, "pending": len(pending)})
    if not pending:
        logger.info("stage B: nothing to read")
        return []

    logger.info("stage B: reading metadata", extra={"pending": len(pending)})
    progress = ProgressReporter("read", total=len(pending), interval_seconds=5.0)
    failures: list[ScanError] = []

    for start in range(0, len(pending), tool.batch_size):
        chunk = pending[start : start + tool.batch_size]
        result = tool.read_many(chunk)
        store.record_metadata(result.records)
        if result.failures:
            store.mark_unreadable([f.path for f in result.failures], "exiftool could not read")
            failures.extend(
                ScanError(source_file=f.path, stage="read", error=f.reason, exiftool_stderr=f.stderr)
                for f in result.failures
            )
        progress.advance(len(chunk), context=str(chunk[-1].parent))

    progress.finish()
    if failures:
        logger.warning("stage B finished with unreadable files", extra={"count": len(failures)})
    return failures


def _stage_c(
    store: ScanStore,
    root: Path,
    out_dir: Path,
    *,
    failures: list[ScanError],
    cluster_threshold: int,
    xmp_enabled: bool,
    limited: bool,
    log: object,
) -> dict[str, int]:
    logger = get_logger()
    logger.info("stage C: classifying")

    pending = store.count(pending_only=True)
    if pending or limited or not store.walk_complete:
        # Clustering compares every file against every other file. A partial
        # scan has not seen the other files yet, so a 400-file cluster can look
        # like a 3-file coincidence and pass as `ok`.
        logger.warning(
            "clustering is unreliable on a partial scan: run without --limit before trusting "
            "date_status=clustered",
            extra={"pending": pending, "limited": limited},
        )

    clusters = store.clusters(threshold=cluster_threshold)
    if clusters:
        logger.info(
            "clusters found",
            extra={
                "distinct_timestamps": len(clusters),
                "files": sum(c.size for c in clusters.values()),
            },
        )

    folder_counts = store.folder_counts()
    folder_cache = _build_folder_cache(folder_counts, root)

    now = datetime.now()
    file_rows: list[dict[str, object]] = []
    progress = ProgressReporter("classify", total=store.count(), interval_seconds=5.0)

    for stored in store.iter_files():
        chain = build_folder_chain(stored.folder, str(root), folder_cache)
        verdict = assess(stored, folder_chain=chain, clusters=clusters, now=now, xmp_enabled=xmp_enabled)
        file_rows.append(
            {
                "SourceFile": stored.real_path,
                "folder_path": stored.folder,
                "filename": stored.filename,
                "date_status": verdict.date_status,
                "existing_date": verdict.existing_date,
                "proposed_date": verdict.proposed_date,
                "date_source": verdict.date_source,
                "date_match": verdict.date_match,
                "confidence": verdict.confidence,
                "existing_keywords": verdict.existing_keywords,
                # Blank at scan time by design: these are override slots. What
                # actually gets written comes from folder inheritance unless the
                # reviewer types something here, and pre-filling them would make
                # an inherited value indistinguishable from a chosen one.
                "proposed_keywords": "",
                "proposed_title": "",
                "scan_size": stored.size,
                "scan_mtime": f"{stored.mtime:.6f}",
                "cluster_size": verdict.cluster_size,
                "cluster_folders": verdict.cluster_folders,
                "date_flags": verdict.date_flags,
                "writable": 1 if verdict.writable else 0,
                "existing_title": verdict.existing_title,
                "date_rule": verdict.date_rule,
                "inherit_title": verdict.inherit_title,
                "inherit_keywords": verdict.inherit_keywords,
                "apply": 1 if verdict.apply else 0,
            }
        )
        progress.advance()
    progress.finish()

    folder_rows = [
        {
            "folder_path": path,
            "file_count": folder_counts.get(path, 0),
            "folder_name": Path(path).name,
            "proposed_title": proposal.title,
            "proposed_keywords": proposal.keywords,
            "proposed_date": format_exif(proposal.date) if proposal.date else "",
            "skip": 1 if proposal.skip else 0,
        }
        for path, proposal in sorted(folder_cache.items())
        if path in folder_counts
    ]

    write_review_csv(out_dir / "files.csv", FileRecord.columns(), file_rows)
    write_review_csv(out_dir / "folders.csv", FolderRecord.columns(), folder_rows)
    write_review_csv(
        out_dir / "scan_errors.csv",
        ScanError.columns(),
        [
            {
                "SourceFile": e.source_file,
                "stage": e.stage,
                "error": e.error,
                "exiftool_stderr": e.exiftool_stderr,
            }
            for e in failures
        ],
    )

    return {
        "files": len(file_rows),
        "folders": len(folder_rows),
        "approved_by_default": sum(1 for r in file_rows if r["apply"] == 1),
        "unwritable": sum(1 for r in file_rows if r["writable"] == 0),
        "errors": len(failures),
    }


def _build_folder_cache(folder_counts: dict[str, int], root: Path) -> dict[str, FolderProposal]:
    """Classify every folder holding images, plus all their ancestors.

    Ancestors are included even when they hold no images themselves: in a tree
    like `Hawaii Trip/2019/DCIM/`, the only folder with a usable name is two
    levels above the photos.
    """
    cache: dict[str, FolderProposal] = {}
    for folder in folder_counts:
        current = Path(folder)
        while True:
            key = str(current)
            if key not in cache:
                cache[key] = classify_folder(current.name)
            if current == root or current.parent == current or root not in current.parents:
                break
            current = current.parent
    return cache


def _print_summary(out_dir: Path, stats: WalkStats, counts: dict[str, int], *, failures_count: int) -> None:
    width = shutil.get_terminal_size((80, 20)).columns
    line = "-" * min(width, 72)
    print(line)
    print(f"  images scanned      {counts['files']:>10,}")
    print(f"  folders             {counts['folders']:>10,}")
    print(f"  approved by default {counts['approved_by_default']:>10,}")
    print(f"  unwritable formats  {counts['unwritable']:>10,}")
    print(f"  videos skipped      {stats.videos_skipped:>10,}")
    print(f"  unreadable files    {failures_count:>10,}")
    print(line)
    print(f"  Review {out_dir / 'files.csv'} and {out_dir / 'folders.csv'},")
    print("  set the `apply` column, then run apply.py. Nothing has been written yet.")
    print(line)


if __name__ == "__main__":
    raise SystemExit(main())
