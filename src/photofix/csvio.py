"""CSV reading and writing, with two encodings that must not be mixed up.

Review CSVs (files.csv, folders.csv, reports) are written **utf-8-sig**. Without
the BOM, Excel on both platforms decodes them as the system codepage and a
folder named "Café" opens as "CafÃ©" — which then gets saved back that way.

applied.csv is written **plain UTF-8, no BOM**, because it is consumed by
exiftool rather than by a human. exiftool reads the BOM as part of the first
header name, turning `SourceFile` into `\ufeffSourceFile`, which matches no tag.
It then processes every row as having no recognized columns and reports success
having written nothing. That failure is completely silent, which is what makes
it worth a paragraph.

Every review CSV is QUOTE_ALL. Excel's export delimiter follows the system
locale (semicolon in much of Europe), and keyword values contain semicolons by
design; quoting everything keeps the round trip intact either way.
"""

from __future__ import annotations

import csv
import os
import sys
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

#: Long paths and large keyword lists can exceed the default 128KB field limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

REVIEW_ENCODING = "utf-8-sig"
EXIFTOOL_ENCODING = "utf-8"


def write_review_csv(
    path: Path,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any] | dict[str, Any]],
) -> int:
    """Write a CSV meant for a human in Excel or Numbers."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding=REVIEW_ENCODING, newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow(
                [_cell(row[c]) for c in columns] if isinstance(row, dict) else [_cell(v) for v in row]
            )
            count += 1
    return count


def write_exiftool_csv(path: Path, columns: Sequence[str], rows: Iterable[dict[str, Any]]) -> int:
    """Write applied.csv for exiftool's `-csv=`.

    Minimal quoting rather than QUOTE_ALL: exiftool's CSV parser is happiest
    with conventional output, and there is no Excel round trip to protect here.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    # Written beside and renamed over: applied.csv accumulates every run, and a
    # full disk or Ctrl-C halfway through a rewrite would lose earlier records.
    partial = path.with_name(path.name + ".partial")
    with partial.open("w", encoding=EXIFTOOL_ENCODING, newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _cell(row.get(c, "")) for c in columns})
            count += 1
    os.replace(partial, path)
    return count


def read_review_csv(path: Path) -> Iterator[dict[str, str]]:
    """Read a reviewed CSV back, tolerating what a spreadsheet did to it.

    `utf-8-sig` strips a BOM if present and is a no-op if not, so a file
    re-saved by Excel, LibreOffice, or Numbers all read the same. Header
    whitespace is stripped because some editors pad columns on save.
    """
    try:
        with path.open("r", encoding=REVIEW_ENCODING, newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                return
            cleaned = [(name or "").strip() for name in reader.fieldnames]
            for raw in reader:
                row = {
                    key: ("" if value is None else str(value))
                    for key, value in zip(
                        cleaned, (raw.get(orig) for orig in reader.fieldnames), strict=False
                    )
                }
                yield row
    except UnicodeDecodeError as exc:
        raise CsvEncodingError(
            f"{path.name} is not saved as UTF-8, so accented folder and file names would be "
            "wrong. In Excel use File > Save As > 'CSV UTF-8 (Comma delimited)'; in Numbers, "
            "Export To > CSV with Unicode (UTF-8) encoding."
        ) from exc


class CsvEncodingError(ValueError):
    """A review CSV was re-saved in a legacy encoding."""


def read_csv_headers(path: Path) -> list[str]:
    with path.open("r", encoding=REVIEW_ENCODING, newline="") as fh:
        reader = csv.reader(fh)
        for first in reader:
            return [cell.strip() for cell in first]
    return []


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
