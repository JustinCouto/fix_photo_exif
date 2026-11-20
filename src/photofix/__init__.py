"""photofix — audit and repair photo metadata via reviewable CSVs and exiftool.

Three phases, each independently re-runnable, with files on disk as the phase
boundaries:

    scan.py    walk the tree, read metadata, propose fixes -> files.csv, folders.csv
    apply.py   read the reviewed CSVs, guard, write metadata in place
    verify.py  re-read the tree and diff against what apply intended

The tool never writes to an image without an explicit approved row, and never
moves, renames, or deletes anything.
"""

from __future__ import annotations

__version__ = "0.1.0"

EXIT_OK = 0
EXIT_ATTENTION = 1  # ran, but something needs a human: conflicts, verify failures
EXIT_USAGE = 2  # usage or validation error; nothing was attempted
