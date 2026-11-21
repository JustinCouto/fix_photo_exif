"""SQLite-backed scan state, so a scan over a large archive is resumable.

Also the baseline that `apply` re-stats against. That second role is why the
invalidation rule in `upsert_walked` matters more than it looks: if a file
changes between two scans and its cached metadata is *not* discarded, apply
would later compare the file against a snapshot that never described it, and
either wave through a change it should have flagged or flag one it should not.

Key fields are extracted into typed, indexed columns rather than left inside
`meta_json`. Clustering needs `GROUP BY` over tens of thousands of rows, and you
cannot index into a JSON blob.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exiftool import READ_VERSION_TAG, normalize_key
from .folders import is_placeholder_title
from .walker import WalkedFile

#: Where an existing capture date can live, in the order Apple Photos prefers.
#: XMP:DateCreated is photoshop:DateCreated, which this tool also writes.
DATE_TAGS: tuple[str, ...] = (
    "EXIF:DateTimeOriginal",
    "XMP:DateTimeOriginal",
    "XMP:DateCreated",
    "IPTC:DateCreated",
)
#: The two fields apply writes, XMP first because IPTC caps titles at 64 bytes,
#: then the Windows Explorer title, which counts as a title the photo already has.
TITLE_TAGS: tuple[str, ...] = ("XMP:Title", "IPTC:ObjectName", "EXIF:XPTitle")
#: Where existing keywords can live. Any of them means "this photo has keywords".
KEYWORD_TAGS: tuple[str, ...] = (
    "IPTC:Keywords",
    "XMP:Subject",
    "XMP:TagsList",
    "XMP:HierarchicalSubject",
    "EXIF:XPKeywords",
)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    path        TEXT PRIMARY KEY,   -- normalize_key() form, matches exiftool's SourceFile
    real_path   TEXT NOT NULL,      -- as the OS gave it, used for all filesystem access
    folder      TEXT NOT NULL,
    filename    TEXT NOT NULL,
    ext         TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    birthtime   REAL,
    inode       INTEGER,
    -- NULL until stage B has read this file. Also the resume marker.
    meta_json   TEXT,
    scanned_at  REAL,
    -- Extracted for indexing and SQL-side grouping.
    datetime_original       TEXT,
    datetime_original_norm  TEXT,
    create_date             TEXT,
    keywords                TEXT,
    title                   TEXT,
    mime_type               TEXT,
    warning                 TEXT,
    error                   TEXT
);

-- Each file's metadata as it was just before apply last wrote it. Written by
-- apply only, so verify can still tell intended changes from unintended ones
-- after a rescan has replaced files.meta_json with post-apply values.
CREATE TABLE IF NOT EXISTS baselines (
    path       TEXT PRIMARY KEY,
    meta_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_dto_norm ON files(datetime_original_norm);
CREATE INDEX IF NOT EXISTS idx_files_folder   ON files(folder);
CREATE INDEX IF NOT EXISTS idx_files_pending  ON files(path) WHERE meta_json IS NULL;
"""


@dataclass(frozen=True, slots=True)
class StoredFile:
    path: str
    real_path: str
    folder: str
    filename: str
    ext: str
    size: int
    mtime: float
    birthtime: float | None
    meta: dict[str, Any]
    datetime_original: str | None
    create_date: str | None
    keywords: str | None
    title: str | None
    mime_type: str | None
    warning: str | None
    error: str | None
    inode: int | None = None

    @property
    def other_dates(self) -> list[tuple[str, str]]:
        """Dates in the other DATE_TAGS, for when `datetime_original` is bad."""
        first = normalize_timestamp(self.datetime_original)
        out = []
        for tag in DATE_TAGS:
            value = _as_text(self.meta.get(tag))
            if value and normalize_timestamp(value) != first:
                out.append((tag, value))
        return out


@dataclass(frozen=True, slots=True)
class Cluster:
    """A timestamp shared by suspiciously many files."""

    normalized: str
    size: int
    folders: int


def normalize_timestamp(value: str | None) -> str | None:
    """Reduce an exiftool timestamp to a groupable key.

    Strips subseconds and any timezone suffix so `2019:07:04 12:00:00`,
    `2019:07:04 12:00:00.00`, and `2019:07:04 12:00:00+01:00` cluster together.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text or text.startswith("0000"):
        return None
    if _DATE_ONLY.match(text):
        # IPTC and XMP dates are often a day with no time. That is a real date,
        # not damage, and it groups with what the tool writes for a known day.
        return f"{text[:10]} 12:00:00"
    head = text[:19]
    return head if len(head) == 19 else text


_DATE_ONLY = re.compile(r"^\d{4}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2}|Z)?$")


def _first(meta: dict[str, Any], *candidates: str) -> Any:
    """Pick the first present group-qualified key.

    With -G0 everything arrives prefixed, and the same logical value can live in
    more than one group (a title is EXIF:ImageDescription, IPTC:ObjectName, or
    XMP:Title depending on what wrote it).
    """
    for key in candidates:
        if key in meta and meta[key] not in (None, "", []):
            return meta[key]
    return None


def _title(meta: dict[str, Any]) -> str | None:
    """The first real title, so an "Untitled" in one field cannot hide a real
    title in the other. All placeholders gives the first, for display."""
    present = [str(meta[k]) for k in TITLE_TAGS if meta.get(k) not in (None, "", [])]
    return next((t for t in present if not is_placeholder_title(t)), present[0] if present else None)


def _as_text(value: Any) -> str | None:
    if value is None or value == [] or value == "":
        return None
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return str(value)


class ScanStore:
    """Owns scan.db. Use as a context manager."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        # WAL so an interrupted scan leaves a readable database rather than a
        # rollback journal someone has to reason about.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(files)")}
        if "inode" not in columns:
            # A scan.db from before inodes were recorded. Its NULLs differ from
            # every walked inode, so each file is re-read once.
            self.conn.execute("ALTER TABLE files ADD COLUMN inode INTEGER")
        self.set_meta("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    def __enter__(self) -> ScanStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.commit()
        finally:
            self.conn.close()

    # -- meta -------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def mark_walk_complete(self, complete: bool = True) -> None:
        self.set_meta("walk_complete", "1" if complete else "0")
        self.conn.commit()

    @property
    def walk_complete(self) -> bool:
        return self.get_meta("walk_complete") == "1"

    # -- stage A ----------------------------------------------------------

    def upsert_walked(self, files: Iterable[WalkedFile]) -> int:
        """Record walked files, discarding cached metadata for changed ones.

        The CASE expressions are the whole point. Inserting only new paths would
        leave a previously-scanned file that has since been edited carrying its
        old `meta_json` forever, and every downstream phase would then be
        reasoning about a file that no longer exists in that form. Any change to
        size, mtime, or inode resets the cache so stage B re-reads it.
        """
        changed = (
            "files.size <> excluded.size OR files.mtime <> excluded.mtime"
            " OR files.inode IS NOT excluded.inode"
        )
        reset = ",\n".join(
            f"{column} = CASE WHEN {changed} THEN NULL ELSE files.{column} END"
            for column in (
                "meta_json",
                "scanned_at",
                "datetime_original",
                "datetime_original_norm",
                "create_date",
                "keywords",
                "title",
                "mime_type",
                "warning",
                "error",
            )
        )
        rows = [
            (
                normalize_key(f.path),
                str(f.path),
                str(f.path.parent),
                f.path.name,
                f.path.suffix.lower(),
                f.size,
                f.mtime,
                f.birthtime,
                f.inode,
            )
            for f in files
        ]
        if not rows:
            return 0
        self.conn.executemany(
            f"""
            INSERT INTO files (path, real_path, folder, filename, ext, size, mtime, birthtime, inode)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                {reset},
                real_path = excluded.real_path,
                size      = excluded.size,
                mtime     = excluded.mtime,
                birthtime = excluded.birthtime,
                inode     = excluded.inode
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    # -- stage B ----------------------------------------------------------

    def pending_paths(self, limit: int | None = None) -> list[Path]:
        """Files still awaiting a metadata read. Resume reads this and nothing else."""
        sql = "SELECT real_path FROM files WHERE meta_json IS NULL ORDER BY path"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [Path(row["real_path"]) for row in self.conn.execute(sql)]

    def record_metadata(self, records: dict[str, dict[str, Any]]) -> None:
        now = time.time()
        rows = []
        for key, meta in records.items():
            dto = _as_text(_first(meta, *DATE_TAGS))
            rows.append(
                (
                    json.dumps(meta, ensure_ascii=False, default=str),
                    now,
                    dto,
                    normalize_timestamp(dto),
                    _as_text(_first(meta, "EXIF:CreateDate", "XMP:CreateDate", "QuickTime:CreateDate")),
                    _as_text(_first(meta, *KEYWORD_TAGS)),
                    _title(meta),
                    _as_text(_first(meta, "File:MIMEType")),
                    _as_text(_first(meta, "ExifTool:Warning")),
                    _as_text(_first(meta, "ExifTool:Error")),
                    key,
                )
            )
        if not rows:
            return
        self.conn.executemany(
            """
            UPDATE files SET
                meta_json = ?, scanned_at = ?,
                datetime_original = ?, datetime_original_norm = ?, create_date = ?,
                keywords = ?, title = ?, mime_type = ?, warning = ?, error = ?
            WHERE path = ?
            """,
            rows,
        )
        self.conn.commit()

    def retry_unreadable(self) -> int:
        """Send files parked as unreadable back to stage B.

        A file that could not be opened last time (permissions, a drive that
        dropped off) may be readable now, and changing its permissions does not
        change its size, mtime, or inode, so nothing else would re-read it.
        """
        cursor = self.conn.execute(
            "UPDATE files SET meta_json = NULL, scanned_at = NULL, error = NULL "
            "WHERE error IS NOT NULL AND meta_json = '{}'"
        )
        self.conn.commit()
        return cursor.rowcount

    def prune(self, keep: set[str]) -> int:
        """Forget files a complete walk no longer found: deleted, moved, or renamed.

        Left in place they would reappear in files.csv, and approving one would
        make apply refuse the whole run because the file cannot be found.
        """
        rows = self.conn.execute("SELECT path FROM files")
        gone = [str(row["path"]) for row in rows if row["path"] not in keep]
        for start in range(0, len(gone), 900):
            chunk = gone[start : start + 900]
            self.conn.execute(f"DELETE FROM files WHERE path IN ({','.join('?' * len(chunk))})", chunk)
        self.conn.commit()
        return len(gone)

    def mark_unreadable(self, paths: Iterable[str], reason: str) -> None:
        """Park files exiftool could not read so resume does not retry forever."""
        rows = [(json.dumps({}), time.time(), reason, normalize_key(p)) for p in paths]
        if not rows:
            return
        self.conn.executemany(
            "UPDATE files SET meta_json=?, scanned_at=?, error=? WHERE path=?",
            rows,
        )
        self.conn.commit()

    def forget_stale_reads(self, version: str) -> int:
        """Queue for re-reading every file read with a different tag list."""
        cursor = self.conn.execute(
            "UPDATE files SET meta_json=NULL WHERE meta_json IS NOT NULL "
            "AND json_extract(meta_json, ?) IS NOT ?",
            (f'$."{READ_VERSION_TAG}"', version),
        )
        self.conn.commit()
        return cursor.rowcount

    # -- queries ----------------------------------------------------------

    def count(self, *, pending_only: bool = False) -> int:
        sql = "SELECT COUNT(*) AS n FROM files"
        if pending_only:
            sql += " WHERE meta_json IS NULL"
        return int(self.conn.execute(sql).fetchone()["n"])

    def clusters(self, *, threshold: int = 20) -> dict[str, Cluster]:
        """Timestamps shared by `threshold`+ files, with their folder spread.

        `folders` is carried because cluster size alone misfires on burst shots:
        forty frames sharing a second inside one folder is a camera burst, while
        forty across thirty folders is a bulk-edit artifact. Only the reviewer
        can tell them apart, so both numbers reach the CSV.
        """
        rows = self.conn.execute(
            """
            SELECT datetime_original_norm AS norm,
                   COUNT(*) AS n,
                   COUNT(DISTINCT folder) AS folders
            FROM files
            WHERE datetime_original_norm IS NOT NULL
            GROUP BY datetime_original_norm
            HAVING COUNT(*) >= ?
            """,
            (threshold,),
        ).fetchall()
        return {str(r["norm"]): Cluster(str(r["norm"]), int(r["n"]), int(r["folders"])) for r in rows}

    def folder_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT folder, COUNT(*) AS n FROM files GROUP BY folder")
        return {str(r["folder"]): int(r["n"]) for r in rows}

    def iter_files(self) -> Iterator[StoredFile]:
        for row in self.conn.execute("SELECT * FROM files ORDER BY path"):
            yield self._to_stored(row)

    def get(self, path: str) -> StoredFile | None:
        row = self.conn.execute("SELECT * FROM files WHERE path=?", (normalize_key(path),)).fetchone()
        return self._to_stored(row) if row else None

    def get_many(self, paths: Iterable[str]) -> dict[str, StoredFile]:
        out: dict[str, StoredFile] = {}
        keys = [normalize_key(p) for p in paths]
        for start in range(0, len(keys), 900):  # under SQLite's variable limit
            chunk = keys[start : start + 900]
            placeholders = ",".join("?" * len(chunk))
            for row in self.conn.execute(f"SELECT * FROM files WHERE path IN ({placeholders})", chunk):
                out[str(row["path"])] = self._to_stored(row)
        return out

    def save_baselines(self, files: Iterable[StoredFile]) -> None:
        """Record what apply is about to change, replacing an earlier apply's record."""
        rows = [(f.path, json.dumps(f.meta, ensure_ascii=False, default=str)) for f in files]
        self.conn.executemany(
            "INSERT INTO baselines(path, meta_json) VALUES(?, ?) "
            "ON CONFLICT(path) DO UPDATE SET meta_json=excluded.meta_json",
            rows,
        )
        self.conn.commit()

    def baselines(self, paths: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Pre-apply metadata, falling back to the scan's for files apply never recorded."""
        keys = [normalize_key(p) for p in paths]
        out = {key: stored.meta for key, stored in self.get_many(keys).items()}
        for start in range(0, len(keys), 900):
            chunk = keys[start : start + 900]
            placeholders = ",".join("?" * len(chunk))
            for row in self.conn.execute(
                f"SELECT path, meta_json FROM baselines WHERE path IN ({placeholders})", chunk
            ):
                try:
                    out[str(row["path"])] = json.loads(row["meta_json"])
                except json.JSONDecodeError:
                    continue
        return out

    @staticmethod
    def _to_stored(row: sqlite3.Row) -> StoredFile:
        raw = row["meta_json"]
        try:
            meta = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            meta = {}
        return StoredFile(
            path=str(row["path"]),
            real_path=str(row["real_path"]),
            folder=str(row["folder"]),
            filename=str(row["filename"]),
            ext=str(row["ext"]),
            size=int(row["size"]),
            mtime=float(row["mtime"]),
            birthtime=float(row["birthtime"]) if row["birthtime"] is not None else None,
            meta=meta,
            datetime_original=row["datetime_original"],
            create_date=row["create_date"],
            keywords=row["keywords"],
            title=row["title"],
            mime_type=row["mime_type"],
            warning=row["warning"],
            error=row["error"],
            inode=int(row["inode"]) if row["inode"] is not None else None,
        )
