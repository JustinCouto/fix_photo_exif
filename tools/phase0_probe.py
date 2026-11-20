"""Phase 0 — confirm the exiftool behaviors the design depends on.

Run this once per machine before trusting `apply.py` against a real archive:

    uv run python tools/phase0_probe.py

Everything here is checked empirically against the installed binary. Behaviors
already settled from `exiftool -h` (empty-cell handling, `-sep`, `-csv+=`, the
command-line file list) are encoded as constraints in the package rather than
re-tested here, with one exception: the empty-cell rule is the single assumption
the whole "blank means don't touch" design rests on, and confirming it costs
three lines.

Exits nonzero if any check fails.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Minimal valid 1x1 images, so the probe needs no fixtures and no dependencies.
SEEDS: dict[str, bytes] = {
    "jpg": base64.b64decode(
        "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
        "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
        "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
    ),
    "png": base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    ),
    "gif": base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"),
    "webp": base64.b64decode("UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoBAAEAAwA0JaQAA3AA/vuUAAA="),
}

EXIFTOOL = shutil.which("exiftool")

failures: list[str] = []
notes: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def note(text: str) -> None:
    print(f"  [note] {text}")
    notes.append(text)


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    assert EXIFTOOL is not None
    return subprocess.run(
        [EXIFTOOL, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def read_tags(path: Path, *tags: str) -> dict[str, Any]:
    args = ["-json", "-G0", "-charset", "filename=utf8", *(f"-{t}" for t in tags), str(path)]
    proc = run(args)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    return dict(data[0]) if data else {}


def write_csv(path: Path, headers: list[str], row: dict[str, str]) -> None:
    """applied.csv-shaped writer: plain UTF-8, no BOM, LF endings."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        writer.writerow(row)


def write_argfile(path: Path, targets: list[Path]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for target in targets:
            fh.write(f"{target}\n")


def apply_csv(
    csv_path: Path, targets: list[Path], workdir: Path, merge: bool = False
) -> subprocess.CompletedProcess[str]:
    argfile = workdir / "targets.args"
    write_argfile(argfile, targets)
    flag = f"-csv+={csv_path}" if merge else f"-csv={csv_path}"
    return run(
        [
            flag,
            "-sep",
            ";",
            "-charset",
            "filename=utf8",
            "-codedcharacterset=utf8",
            "-P",
            "-@",
            str(argfile),
        ]
    )


def seed(workdir: Path, name: str, kind: str) -> Path:
    path = workdir / f"{name}.{kind}"
    path.write_bytes(SEEDS[kind])
    return path


def main() -> int:
    if EXIFTOOL is None:
        print("exiftool not found on PATH. On Windows, restart the shell after installing.")
        return 2

    print(f"exiftool: {EXIFTOOL}")
    ver_proc = run(["-ver"])
    ver_text = ver_proc.stdout.strip()
    print(f"version:  {ver_text}\n")

    print("Version floor")
    try:
        ver_ok = float(ver_text) >= 12.0
    except ValueError:
        ver_ok = False
    check("exiftool -ver >= 12.0", ver_ok, ver_text)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)

        # ------------------------------------------------------------------
        print("\nItem 7 — _original backup naming and placement")
        target = seed(workdir, "backup", "jpg")
        pristine = hashlib.sha256(target.read_bytes()).hexdigest()

        csv_path = workdir / "one.csv"
        write_csv(
            csv_path,
            ["SourceFile", "EXIF:DateTimeOriginal"],
            {"SourceFile": str(target), "EXIF:DateTimeOriginal": "2019:07:04 12:00:00"},
        )
        proc = apply_csv(csv_path, [target], workdir)
        backup = target.with_name(target.name + "_original")
        check("backup is named FILE_original beside the original", backup.exists(), str(backup.name))
        check(
            "backup holds the pristine pre-tool bytes",
            backup.exists() and hashlib.sha256(backup.read_bytes()).hexdigest() == pristine,
        )
        check(
            "the date landed",
            read_tags(target, "EXIF:DateTimeOriginal").get("EXIF:DateTimeOriginal") == "2019:07:04 12:00:00",
            str(proc.returncode),
        )

        # ------------------------------------------------------------------
        print("\nItem 11 — a second write to an already-written file")
        size_after_first = target.stat().st_size
        write_csv(
            csv_path,
            ["SourceFile", "EXIF:DateTimeOriginal"],
            {"SourceFile": str(target), "EXIF:DateTimeOriginal": "2021:01:02 03:04:05"},
        )
        proc2 = apply_csv(csv_path, [target], workdir)
        second_backup = target.with_name(target.name + "_original_original")
        check(
            "second write does NOT error (the plan's correction is right)",
            proc2.returncode == 0,
            f"rc={proc2.returncode} {proc2.stderr.strip()[:80]}",
        )
        check(
            "no FILE_original_original is created",
            not second_backup.exists(),
        )
        check(
            "the FIRST backup still holds the pristine bytes",
            hashlib.sha256(backup.read_bytes()).hexdigest() == pristine,
        )
        check(
            "the second write actually took",
            read_tags(target, "EXIF:DateTimeOriginal").get("EXIF:DateTimeOriginal") == "2021:01:02 03:04:05",
        )
        note(
            "confirmed: nothing errors or warns on a re-write, so the Phase 2 stat "
            "guard is the sole protection against a second write"
        )
        if target.stat().st_size != size_after_first:
            note(f"file size changed again on re-write ({size_after_first} -> {target.stat().st_size})")

        # ------------------------------------------------------------------
        print("\nItem 8 — accented values survive a -csv= round trip")
        accented = seed(workdir, "accents", "jpg")
        write_csv(
            csv_path,
            ["SourceFile", "IPTC:Keywords", "XMP:Subject", "IPTC:ObjectName"],
            {
                "SourceFile": str(accented),
                "IPTC:Keywords": "Café;Zürich",
                "XMP:Subject": "Café;Zürich",
                "IPTC:ObjectName": "Noël",
            },
        )
        apply_csv(csv_path, [accented], workdir)
        got = read_tags(accented, "IPTC:Keywords", "XMP:Subject", "IPTC:ObjectName")
        iptc_kw = got.get("IPTC:Keywords")
        xmp_subj = got.get("XMP:Subject")
        check(
            "IPTC:Keywords round-trips accents and splits on ';'",
            iptc_kw == ["Café", "Zürich"],
            repr(iptc_kw),
        )
        check(
            "XMP:Subject round-trips accents and splits on ';'",
            xmp_subj == ["Café", "Zürich"],
            repr(xmp_subj),
        )
        check(
            "IPTC:ObjectName round-trips accents",
            got.get("IPTC:ObjectName") == "Noël",
            repr(got.get("IPTC:ObjectName")),
        )

        # ------------------------------------------------------------------
        print("\nItem 9 — group-qualified CSV headers land only in their named group")
        grouped = seed(workdir, "groups", "jpg")
        write_csv(
            csv_path,
            ["SourceFile", "EXIF:DateTimeOriginal", "IPTC:Keywords", "XMP:Subject"],
            {
                "SourceFile": str(grouped),
                "EXIF:DateTimeOriginal": "2005:06:07 08:09:10",
                "IPTC:Keywords": "alpha",
                "XMP:Subject": "beta",
            },
        )
        apply_csv(csv_path, [grouped], workdir)
        everything = read_tags(grouped, "DateTimeOriginal", "Keywords", "Subject")
        check(
            "EXIF:DateTimeOriginal landed in EXIF",
            everything.get("EXIF:DateTimeOriginal") == "2005:06:07 08:09:10",
            repr(everything.get("EXIF:DateTimeOriginal")),
        )
        check("IPTC:Keywords landed in IPTC", everything.get("IPTC:Keywords") == "alpha")
        check("XMP:Subject landed in XMP", everything.get("XMP:Subject") == "beta")
        check(
            "Keywords did NOT bleed into XMP",
            everything.get("XMP:Keywords") is None,
            repr(everything.get("XMP:Keywords")),
        )
        check(
            "Subject did NOT bleed into IPTC",
            everything.get("IPTC:Subject") is None,
            repr(everything.get("IPTC:Subject")),
        )

        # ------------------------------------------------------------------
        print("\nBonus — empty cells are ignored, not treated as deletions")
        # This is settled from the docs, but it is the assumption the entire
        # "blank means don't touch" design rests on, so it is worth three lines.
        keep = seed(workdir, "blank", "jpg")
        write_csv(
            csv_path,
            ["SourceFile", "EXIF:DateTimeOriginal", "IPTC:Keywords"],
            {
                "SourceFile": str(keep),
                "EXIF:DateTimeOriginal": "2010:10:10 10:10:10",
                "IPTC:Keywords": "keepme",
            },
        )
        apply_csv(csv_path, [keep], workdir)
        write_csv(
            csv_path,
            ["SourceFile", "EXIF:DateTimeOriginal", "IPTC:Keywords"],
            {"SourceFile": str(keep), "EXIF:DateTimeOriginal": "2011:11:11 11:11:11", "IPTC:Keywords": ""},
        )
        apply_csv(csv_path, [keep], workdir)
        after = read_tags(keep, "EXIF:DateTimeOriginal", "IPTC:Keywords")
        check(
            "a blank cell left the existing keyword intact",
            after.get("IPTC:Keywords") == "keepme",
            repr(after.get("IPTC:Keywords")),
        )
        check(
            "the non-blank cell in the same row still updated",
            after.get("EXIF:DateTimeOriginal") == "2011:11:11 11:11:11",
        )

        # ------------------------------------------------------------------
        print("\nItem 10 — per-tag acceptance on .gif / .webp / .png")
        capability: dict[str, dict[str, bool]] = {}
        for kind in ("png", "webp", "gif"):
            probe = seed(workdir, f"cap_{kind}", kind)
            write_csv(
                csv_path,
                ["SourceFile", "EXIF:DateTimeOriginal", "XMP:DateCreated", "IPTC:Keywords", "XMP:Subject"],
                {
                    "SourceFile": str(probe),
                    "EXIF:DateTimeOriginal": "2012:03:04 05:06:07",
                    "XMP:DateCreated": "2012:03:04 05:06:07",
                    "IPTC:Keywords": "kw",
                    "XMP:Subject": "kw",
                },
            )
            result = apply_csv(csv_path, [probe], workdir)
            landed = read_tags(
                probe, "EXIF:DateTimeOriginal", "XMP:DateCreated", "IPTC:Keywords", "XMP:Subject"
            )
            capability[kind] = {
                "EXIF:DateTimeOriginal": landed.get("EXIF:DateTimeOriginal") is not None,
                "XMP:DateCreated": landed.get("XMP:DateCreated") is not None,
                "IPTC:Keywords": landed.get("IPTC:Keywords") is not None,
                "XMP:Subject": landed.get("XMP:Subject") is not None,
            }
            accepted = sorted(t for t, ok in capability[kind].items() if ok)
            rejected = sorted(t for t, ok in capability[kind].items() if not ok)
            print(f"  .{kind}: accepted={accepted or 'NONE'}")
            if rejected:
                print(f"         rejected={rejected}")
            if result.stderr.strip():
                print(f"         stderr={result.stderr.strip()[:160]}")

        check(
            "at least one date tag lands on .gif (so it is not silently a no-op)",
            capability["gif"]["EXIF:DateTimeOriginal"] or capability["gif"]["XMP:DateCreated"],
        )
        if not capability["gif"]["EXIF:DateTimeOriginal"]:
            note("confirmed: .gif rejects EXIF:DateTimeOriginal; its date must go to XMP:DateCreated")
        if capability["gif"]["EXIF:DateTimeOriginal"]:
            note("SURPRISE: .gif accepted EXIF:DateTimeOriginal — formats.py may be over-restrictive")

        print("\n  capability matrix (for formats.py):")
        print("  " + json.dumps(capability, indent=2).replace("\n", "\n  "))

    print()
    if failures:
        print(f"FAILED {len(failures)} check(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"All checks passed ({len(notes)} note(s) recorded).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
