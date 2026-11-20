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
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exiftool import normalize_key
from .walker import WalkedFile

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
    head = text[:19]
    return head if len(head) == 19 else text


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
        size or mtime resets the cache so stage B re-reads it.
        """
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
            )
            for f in files
        ]
        if not rows:
            return 0
        self.conn.executemany(
            """
            INSERT INTO files (path, real_path, folder, filename, ext, size, mtime, birthtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                real_path = excluded.real_path,
                size      = excluded.size,
                mtime     = excluded.mtime,
                birthtime = excluded.birthtime,
                meta_json = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.meta_json END,
                scanned_at = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.scanned_at END,
                datetime_original = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.datetime_original END,
                datetime_original_norm = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.datetime_original_norm END,
                create_date = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.create_date END,
                keywords = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.keywords END,
                title = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.title END,
                mime_type = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.mime_type END,
                warning = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.warning END,
                error = CASE
                    WHEN files.size <> excluded.size OR files.mtime <> excluded.mtime
                    THEN NULL ELSE files.error END
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
            dto = _as_text(_first(meta, "EXIF:DateTimeOriginal", "XMP:DateTimeOriginal"))
            rows.append(
                (
                    json.dumps(meta, ensure_ascii=False, default=str),
                    now,
                    dto,
                    normalize_timestamp(dto),
                    _as_text(_first(meta, "EXIF:CreateDate", "XMP:CreateDate", "QuickTime:CreateDate")),
                    _as_text(_first(meta, "IPTC:Keywords", "XMP:Subject", "XMP:TagsList")),
                    _as_text(
                        _first(
                            meta,
                            "IPTC:ObjectName",
                            "XMP:Title",
                            "EXIF:ImageDescription",
                            "IPTC:Headline",
                        )
                    ),
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
        )
