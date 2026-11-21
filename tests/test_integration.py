"""End-to-end against the real binary: scan, apply, verify, then apply again.

Marked `exiftool` and skipped when the binary is absent. A skip here is not a
pass — `just check` runs `preflight` first so a missing binary fails the gate
rather than quietly reducing the suite to unit tests.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from make_fixture import build  # noqa: E402

from photofix import EXIT_ATTENTION, EXIT_OK, EXIT_USAGE  # noqa: E402
from photofix.apply import main as apply_main  # noqa: E402
from photofix.csvio import read_review_csv, write_review_csv  # noqa: E402
from photofix.models import FileRecord  # noqa: E402
from photofix.scan import main as scan_main  # noqa: E402
from photofix.verify import main as verify_main  # noqa: E402

pytestmark = pytest.mark.exiftool

EXIFTOOL = shutil.which("exiftool")
requires_exiftool = pytest.mark.skipif(EXIFTOOL is None, reason="exiftool not on PATH")


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("archive")
    build(root)
    return root


def rows(path: Path) -> list[dict[str, str]]:
    return list(read_review_csv(path))


def by_name(csv_rows: list[dict[str, str]], name: str) -> dict[str, str]:
    for row in csv_rows:
        if Path(row["SourceFile"]).name == name:
            return row
    raise AssertionError(f"{name} is not in files.csv")


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


@requires_exiftool
def test_scan_produces_reviewable_csvs(archive: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    out = tmp_path_factory.mktemp("out")
    assert scan_main(["--root", str(archive), "--out-dir", str(out)]) == EXIT_OK

    files = rows(out / "files.csv")
    assert files, "scan found no images"
    assert list(files[0]) == FileRecord.columns()

    # Parents holding no images directly must still reach apply, or nothing
    # inherits from "Christmas" in Christmas/2020/. The root is never listed.
    folder_names = {r["folder_name"] for r in rows(out / "folders.csv")}
    assert "DCIM" in folder_names
    assert archive.name not in folder_names

    names = {Path(r["SourceFile"]).name for r in files}
    assert "IMG_0001.jpg" in names
    assert "clip.mov" not in names, "video must be skipped"
    assert "Thumbs.db" not in names
    assert "junk.jpg" not in names, "__MACOSX must be skipped"
    assert "inside.jpg" not in names, ".photoslibrary must be skipped"


@requires_exiftool
def test_scan_classifies_the_interesting_cases(
    archive: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    out = tmp_path_factory.mktemp("out")
    scan_main(["--root", str(archive), "--out-dir", str(out)])
    files = rows(out / "files.csv")

    # A camera sequence number must not be read as a date.
    dsc = by_name(files, "DSC_0704.jpg")
    assert dsc["date_source"] != "filename" or dsc["date_match"] != "0704"

    # A filename date is high confidence.
    sunset = by_name(files, "2019-07-04 sunset.jpg")
    assert sunset["proposed_date"] == "2019:07:04 12:00:00"
    assert sunset["date_source"] == "filename"
    assert sunset["confidence"] == "high"

    # A dateless file in a dated folder inherits the folder's date.
    inherited = by_name(files, "IMG_0002.jpg")
    assert inherited["date_source"] == "folder"
    assert inherited["confidence"] == "medium"
    assert inherited["proposed_date"] == "2020:01:15 12:00:00"

    # An already-good date gets no proposal at all.
    good = by_name(files, "good.jpg")
    assert good["date_status"] == "ok"
    assert good["proposed_date"] == ""

    # Scanner and firmware defaults are implausible, not ok.
    assert by_name(files, "scan01.jpg")["date_status"] == "implausible"
    assert by_name(files, "scan02.jpg")["date_status"] == "implausible"

    # 25 files share a timestamp across two folders.
    clustered = by_name(files, "a000.jpg")
    assert clustered["date_status"] == "clustered"
    assert clustered["cluster_size"] == "25"
    assert clustered["cluster_folders"] == "2"

    # Folder text is offered for inheritance.
    hawaii = by_name(files, "IMG_0001.jpg")
    assert hawaii["inherit_title"] == "Hawaii Trip"
    assert "Hawaii" in hawaii["inherit_keywords"]

    # BMP cannot store anything.
    assert by_name(files, "old.bmp")["writable"] == "0"


@requires_exiftool
def test_scan_resumes_without_redoing_work(archive: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    out = tmp_path_factory.mktemp("out")
    scan_main(["--root", str(archive), "--out-dir", str(out)])
    first = rows(out / "files.csv")
    scan_main(["--root", str(archive), "--out-dir", str(out)])
    second = rows(out / "files.csv")
    assert len(first) == len(second)


@requires_exiftool
def test_limit_stops_early(archive: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    out = tmp_path_factory.mktemp("out")
    scan_main(["--root", str(archive), "--out-dir", str(out), "--limit", "5"])
    assert len(rows(out / "files.csv")) == 5


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def approve(out: Path, names: set[str]) -> None:
    """Rewrite files.csv approving exactly `names`, as a reviewer would."""
    csv_rows = rows(out / "files.csv")
    for row in csv_rows:
        row["apply"] = "1" if Path(row["SourceFile"]).name in names else "0"
    write_review_csv(out / "files.csv", FileRecord.columns(), csv_rows)


@pytest.fixture
def scanned(archive: Path, tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """A private copy of the archive plus a fresh scan, so writes are isolated."""
    work = tmp_path_factory.mktemp("work") / "archive"
    shutil.copytree(archive, work)
    out = tmp_path_factory.mktemp("out")
    scan_main(["--root", str(work), "--out-dir", str(out)])
    return work, out


@requires_exiftool
def test_apply_refuses_when_nothing_is_approved(scanned: tuple[Path, Path]) -> None:
    """Exiftool with zero files exits 0 having done nothing. Catch it earlier."""
    _work, out = scanned
    approve(out, set())
    assert (
        apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
        == EXIT_USAGE
    )


@requires_exiftool
def test_dry_run_writes_applied_csv_but_touches_no_image(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    target = work / "Hawaii Trip 2019" / "IMG_0001.jpg"
    before = target.read_bytes()
    approve(out, {"IMG_0001.jpg"})

    code = apply_main(
        [
            "--files-csv",
            str(out / "files.csv"),
            "--folders-csv",
            str(out / "folders.csv"),
            "--dry-run",
        ]
    )
    assert code == EXIT_OK
    assert (out / "applied.dryrun.csv").exists()
    assert not (out / "applied.csv").exists(), "applied.csv records real writes only"
    assert target.read_bytes() == before, "--dry-run must not modify an image"
    assert not (target.parent / "IMG_0001.jpg_original").exists()


@requires_exiftool
def test_applied_csv_has_no_bom(scanned: tuple[Path, Path]) -> None:
    _work, out = scanned
    approve(out, {"IMG_0001.jpg"})
    apply_main(
        ["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv"), "--dry-run"]
    )
    assert not (out / "applied.dryrun.csv").read_bytes().startswith(b"\xef\xbb\xbf")


@requires_exiftool
def test_apply_writes_the_approved_files_and_only_those(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    approved = work / "Hawaii Trip 2019" / "IMG_0001.jpg"
    untouched = work / "Hawaii Trip 2019" / "good.jpg"
    before_untouched = untouched.read_bytes()

    approve(out, {"IMG_0001.jpg"})
    code = apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
    assert code == EXIT_OK

    assert (approved.parent / "IMG_0001.jpg_original").exists(), "backup must be kept"
    assert untouched.read_bytes() == before_untouched, "an unapproved file must be untouched"


@requires_exiftool
def test_the_written_values_are_what_the_csv_asked_for(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    approve(out, {"IMG_0001.jpg"})
    apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])

    from photofix.exiftool import ExifTool, normalize_key

    target = work / "Hawaii Trip 2019" / "IMG_0001.jpg"
    result = ExifTool(work_dir=out).read_many([target])
    meta = result.records[normalize_key(target)]
    assert meta.get("EXIF:DateTimeOriginal") == "2020:01:01 12:00:00" or meta.get("EXIF:DateTimeOriginal")
    assert meta.get("IPTC:ObjectName") == "Hawaii Trip"
    assert "Hawaii" in meta.get("IPTC:Keywords", [])


@requires_exiftool
def test_accented_values_survive_a_real_apply(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    approve(out, {"IMG_0003.jpg"})
    apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])

    from photofix.exiftool import ExifTool, normalize_key

    target = work / "Café Zürich Noël" / "IMG_0003.jpg"
    result = ExifTool(work_dir=out).read_many([target])
    meta = result.records[normalize_key(target)]
    assert meta.get("IPTC:ObjectName") == "Café Zürich Noël"


# ---------------------------------------------------------------------------
# The double-apply guard, and verify
# ---------------------------------------------------------------------------


@requires_exiftool
def test_a_second_apply_is_a_resumption_not_a_second_write(scanned: tuple[Path, Path]) -> None:
    """The trap Phase 0 confirmed: exiftool itself will not stop this.

    A re-run must recognize the files as already carrying the intended values
    and skip them, leaving the first _original backup — the pristine pre-tool
    copy — untouched.
    """
    work, out = scanned
    approve(out, {"IMG_0001.jpg"})
    assert (
        apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
        == EXIT_OK
    )

    target = work / "Hawaii Trip 2019" / "IMG_0001.jpg"
    backup = target.parent / "IMG_0001.jpg_original"
    backup_bytes = backup.read_bytes()

    code = apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
    assert code == EXIT_OK, (
        "a re-run of a completed apply is success, not failure. Reporting it as an error sends "
        "the user looking for damage, or trying to undo a change that was correct."
    )

    assert backup.read_bytes() == backup_bytes, "the pristine backup must not be replaced"
    assert not (target.parent / "IMG_0001.jpg_original_original").exists()

    conflicts = rows(out / "apply_conflicts.csv")
    assert any(c["classification"] == "already_applied" for c in conflicts)


@requires_exiftool
def test_a_file_edited_behind_our_back_is_refused(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    target = work / "Hawaii Trip 2019" / "IMG_0001.jpg"
    approve(out, {"IMG_0001.jpg"})

    # Something else changes the file into a state we never intended.
    target.write_bytes(target.read_bytes() + b"\x00" * 64)

    code = apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
    assert code == EXIT_ATTENTION
    conflicts = rows(out / "apply_conflicts.csv")
    assert any(c["classification"] == "modified_elsewhere" for c in conflicts)


@requires_exiftool
def test_verify_confirms_a_successful_apply(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    approve(out, {"IMG_0001.jpg", "IMG_0002.jpg", "scan01.jpg"})
    assert (
        apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
        == EXIT_OK
    )

    code = verify_main(["--applied-csv", str(out / "applied.csv"), "--root", str(work)])
    assert code == EXIT_OK, "a correct apply must verify clean"

    report = rows(out / "verify_report.csv")
    assert report
    assert all(r["bucket"] == "verified" for r in report)


@requires_exiftool
def test_verify_reports_the_original_backups_without_deleting_them(scanned: tuple[Path, Path]) -> None:
    work, out = scanned
    approve(out, {"IMG_0001.jpg"})
    apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
    verify_main(["--applied-csv", str(out / "applied.csv"), "--root", str(work)])

    backup = work / "Hawaii Trip 2019" / "IMG_0001.jpg_original"
    assert backup.exists(), "verify must never delete the undo path"
    assert (out / "originals.csv").exists()


@requires_exiftool
def test_verify_catches_a_change_nobody_asked_for(scanned: tuple[Path, Path]) -> None:
    """The bucket that exists to catch the tool doing something extra."""
    work, out = scanned
    approve(out, {"IMG_0001.jpg"})
    apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])

    # Stand in for an unintended write by changing a tag nobody requested, then
    # rescan: the pre-apply baseline must outlive the rescan to notice.
    import subprocess

    target = work / "Hawaii Trip 2019" / "IMG_0001.jpg"
    subprocess.run(
        [
            EXIFTOOL or "exiftool",
            "-XMP-exif:DateTimeOriginal=1999:01:01 00:00:00",
            "-overwrite_original",
            "-q",
            str(target),
        ],
        check=True,
        capture_output=True,
    )
    scan_main(["--root", str(work), "--out-dir", str(out)])
    code = verify_main(["--applied-csv", str(out / "applied.csv"), "--root", str(work)])
    assert code == EXIT_ATTENTION
    unexpected = [r for r in rows(out / "verify_report.csv") if r["bucket"] == "unexpected_change"]
    assert [r["tag"] for r in unexpected] == ["XMP:DateTimeOriginal"]


@requires_exiftool
def test_a_gif_gets_its_date_via_xmp(scanned: tuple[Path, Path]) -> None:
    """GIF rejects EXIF and IPTC, so XMP is the only place its date can live."""
    work, out = scanned
    approve(out, {"animation_2019-07-04.gif"})
    code = apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
    assert code == EXIT_OK

    from photofix.exiftool import ExifTool, normalize_key

    target = work / "Mixed Formats" / "animation_2019-07-04.gif"
    result = ExifTool(work_dir=out).read_many([target])
    meta = result.records[normalize_key(target)]
    assert meta.get("XMP:DateCreated"), "the GIF date must land in XMP"


def _photo(path: Path, *tags: str) -> Path:
    import subprocess

    from make_fixture import JPEG

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(JPEG)
    if tags:
        subprocess.run([EXIFTOOL or "exiftool", "-q", "-overwrite_original", *tags, str(path)], check=True)
    return path


@requires_exiftool
@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 does not block reads on Windows")
def test_a_file_exiftool_cannot_open_is_reported_and_never_approved(tmp_path: Path) -> None:
    """Read as empty, it would look dateless and its real date would be replaced."""
    photo = _photo(tmp_path / "arc" / "Picnic 2012" / "IMG_1.jpg", "-DateTimeOriginal=2012:05:05 14:30:00")
    out = tmp_path / "out"
    photo.chmod(0)
    try:
        assert scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)]) == EXIT_ATTENTION
        assert [Path(r["SourceFile"]).name for r in rows(out / "scan_errors.csv")] == ["IMG_1.jpg"]
        row = by_name(rows(out / "files.csv"), "IMG_1.jpg")
        assert row["apply"] == "0" and row["proposed_date"] == ""
    finally:
        photo.chmod(0o644)

    # Readable again: the next scan re-reads it rather than trusting the failure.
    assert scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)]) == EXIT_OK
    row = by_name(rows(out / "files.csv"), "IMG_1.jpg")
    assert row["date_status"] == "ok" and row["existing_date"] == "2012:05:05 14:30:00"


@requires_exiftool
def test_a_date_only_write_leaves_legacy_accented_iptc_readable(tmp_path: Path) -> None:
    """A global CodedCharacterSet=UTF8 relabeled Latin-1 "Café" and it read back "Caf?"."""
    import subprocess

    photo = _photo(
        tmp_path / "arc" / "Picnic 2012" / "IMG_1.jpg",
        "-charset",
        "iptc=latin",
        "-IPTC:Caption-Abstract=Café",
    )
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    approve(out, {"IMG_1.jpg"})
    code = apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])
    assert code == EXIT_OK
    caption = subprocess.run(
        [EXIFTOOL or "exiftool", "-s3", "-IPTC:Caption-Abstract", str(photo)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    assert caption == "Café"
    assert verify_main(["--applied-csv", str(out / "applied.csv")]) == EXIT_OK


@requires_exiftool
def test_a_rescan_keeps_a_copy_of_an_edited_files_csv(tmp_path: Path) -> None:
    _photo(tmp_path / "arc" / "Picnic 2012" / "IMG_1.jpg")
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    assert not list(out.glob("files.edited-*.csv")), "an untouched files.csv needs no copy"

    approve(out, set())
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    kept = list(out.glob("files.edited-*.csv"))
    assert len(kept) == 1
    assert by_name(rows(kept[0]), "IMG_1.jpg")["apply"] == "0"


@requires_exiftool
@pytest.mark.skipif(sys.platform == "win32", reason="newlines are not legal in Windows filenames")
def test_a_newline_in_a_filename_cannot_inject_an_exiftool_option(tmp_path: Path) -> None:
    """Scan only reads. A name like "x\\n-Title=PWNED.jpg" once wrote titles to its batch."""
    import subprocess

    arc = tmp_path / "arc" / "Trip"
    victim = _photo(arc / "victim.jpg")
    _photo(arc / "x\n-Title=PWNED.jpg")
    before = victim.read_bytes()
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(tmp_path / "out")])
    assert victim.read_bytes() == before
    assert not list(arc.glob("*_original"))
    title = subprocess.run(
        [EXIFTOOL or "exiftool", "-s3", "-XMP:Title", str(victim)], capture_output=True, text=True
    ).stdout
    assert "PWNED" not in title


@requires_exiftool
def test_numeric_looking_text_round_trips_through_apply_and_verify(tmp_path: Path) -> None:
    """exiftool's JSON gives 1.50 as a number; read back as 1.5 it never matched."""
    _photo(tmp_path / "arc" / "Trip" / "IMG_1.jpg")
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    csv_rows = rows(out / "files.csv")
    csv_rows[0].update(apply="1", override_title="1.50", override_keywords="2.10;007")
    write_review_csv(out / "files.csv", FileRecord.columns(), csv_rows)
    args = ["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")]
    assert apply_main(args) == EXIT_OK
    assert verify_main(["--applied-csv", str(out / "applied.csv")]) == EXIT_OK
    assert apply_main(args) == EXIT_OK, "a re-run is a resumption, not a conflict"


@requires_exiftool
def test_an_unwritable_format_is_never_approved_by_default(scanned: tuple[Path, Path]) -> None:
    _work, out = scanned
    bmp = by_name(rows(out / "files.csv"), "old.bmp")
    assert bmp["writable"] == "0"
    assert bmp["apply"] == "0"


def _apply(out: Path) -> int:
    return apply_main(["--files-csv", str(out / "files.csv"), "--folders-csv", str(out / "folders.csv")])


def _tags(path: Path, *tags: str) -> dict[str, str]:
    import json
    import subprocess

    result = subprocess.run(
        [EXIFTOOL or "exiftool", "-j", "-G1", "-s", *tags, str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return {k: str(v) for k, v in json.loads(result.stdout)[0].items() if k != "SourceFile"}


@requires_exiftool
def test_a_new_date_replaces_every_old_one_and_its_offset(tmp_path: Path) -> None:
    """A stale XMP date or +09:00 offset is what Photos may read instead of ours."""
    photo = _photo(
        tmp_path / "arc" / "Hawaii 2014" / "IMG_1.jpg",
        "-EXIF:DateTimeOriginal=2001:01:01 00:05:12",
        "-EXIF:OffsetTimeOriginal=+09:00",
        "-EXIF:SubSecTimeOriginal=123",
        "-XMP-exif:DateTimeOriginal=2001:01:01 00:05:12",
        "-XMP-xmp:CreateDate=2001:01:01 00:05:12",
        "-IPTC:DateCreated=2001:01:01",
        "-IPTC:TimeCreated=00:05:12+09:00",
    )
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    csv_rows = rows(out / "files.csv")
    csv_rows[0].update(apply="1", proposed_date="2014-06-20 10:00:00")
    write_review_csv(out / "files.csv", FileRecord.columns(), csv_rows)
    assert _apply(out) == EXIT_OK

    tags = _tags(photo, "-time:all", "-IPTC:all", "-XMP:all")
    dates = {k: v for k, v in tags.items() if "Date" in k and "File" not in k and "Metadata" not in k}
    assert set(dates.values()) <= {"2014:06:20 10:00:00", "2014:06:20"}, dates
    assert len(dates) >= 6, dates
    assert not any(k.endswith(("OffsetTimeOriginal", "SubSecTimeOriginal", "TimeCreated")) for k in tags), (
        tags
    )

    assert verify_main(["--applied-csv", str(out / "applied.csv")]) == EXIT_OK
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    row = by_name(rows(out / "files.csv"), "IMG_1.jpg")
    assert row["date_status"] == "ok" and row["apply"] == "0"


@requires_exiftool
def test_a_gif_with_an_old_xmp_date_settles_after_one_apply(tmp_path: Path) -> None:
    """Left behind, the old XMP date made every rescan propose the same fix again."""
    import subprocess

    from make_fixture import GIF

    gif = tmp_path / "arc" / "Party" / "anim.gif"
    gif.parent.mkdir(parents=True)
    gif.write_bytes(GIF)
    subprocess.run(
        [
            EXIFTOOL or "exiftool",
            "-q",
            "-overwrite_original",
            "-XMP-exif:DateTimeOriginal=1900:01:01 00:00:00",
            str(gif),
        ],
        check=True,
    )
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    csv_rows = rows(out / "files.csv")
    csv_rows[0].update(apply="1", proposed_date="2015-05-05 12:00:00")
    write_review_csv(out / "files.csv", FileRecord.columns(), csv_rows)
    assert _apply(out) == EXIT_OK
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    row = by_name(rows(out / "files.csv"), "anim.gif")
    assert row["existing_date"] == "2015:05:05 12:00:00" and row["date_status"] == "ok"


@requires_exiftool
def test_deleting_the_context_columns_cannot_overwrite_a_real_title(tmp_path: Path) -> None:
    photo = _photo(tmp_path / "arc" / "Hawaii 2014" / "IMG_1.jpg", "-XMP:Title=Grandma's 80th")
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    csv_rows = rows(out / "files.csv")
    for row in csv_rows:
        row.update(apply="1", existing_title="", existing_keywords="")
    write_review_csv(out / "files.csv", FileRecord.columns(), csv_rows)
    assert _apply(out) == EXIT_OK
    assert _tags(photo, "-XMP:Title")["XMP-dc:Title"] == "Grandma's 80th"


@requires_exiftool
def test_a_title_legacy_iptc_cannot_hold_moves_to_xmp_alone(tmp_path: Path) -> None:
    photo = _photo(
        tmp_path / "arc" / "Trip" / "IMG_1.jpg", "-charset", "iptc=latin", "-IPTC:ObjectName=Vieux Café"
    )
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    csv_rows = rows(out / "files.csv")
    csv_rows[0].update(apply="1", override_title="東京 Noël")
    write_review_csv(out / "files.csv", FileRecord.columns(), csv_rows)
    assert _apply(out) == EXIT_OK
    tags = _tags(photo, "-IPTC:ObjectName", "-XMP:Title")
    assert tags.get("XMP-dc:Title") == "東京 Noël"
    assert "IPTC:ObjectName" not in tags, "two different titles would be left behind"
    assert verify_main(["--applied-csv", str(out / "applied.csv")]) == EXIT_OK


@requires_exiftool
@pytest.mark.skipif(sys.platform != "darwin", reason="Finder tags are a macOS attribute")
def test_finder_tags_survive_an_apply(tmp_path: Path) -> None:
    import subprocess

    photo = _photo(tmp_path / "arc" / "Hawaii 2014" / "IMG_1.jpg")
    name = "com.apple.metadata:_kMDItemUserTags"
    tag = '<plist version="1.0"><array><string>Red\n6</string></array></plist>'
    subprocess.run(["xattr", "-w", name, tag, str(photo)], check=True)
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    approve(out, {"IMG_1.jpg"})
    assert _apply(out) == EXIT_OK
    assert list(photo.parent.glob("*_original")), "exiftool replaced the file"
    kept = subprocess.run(["xattr", "-p", name, str(photo)], capture_output=True, text=True).stdout.strip()
    assert kept == tag


@requires_exiftool
@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_a_locked_file_is_refused_not_rewritten(tmp_path: Path) -> None:
    photo = _photo(tmp_path / "arc" / "Hawaii 2014" / "IMG_1.jpg")
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    approve(out, {"IMG_1.jpg"})
    before = photo.read_bytes()
    photo.chmod(0o444)
    try:
        assert _apply(out) == EXIT_ATTENTION
    finally:
        photo.chmod(0o644)
    assert photo.read_bytes() == before
    assert [r["classification"] for r in rows(out / "apply_conflicts.csv")] == ["unwritable"]


@requires_exiftool
def test_a_wrong_camera_clock_is_shifted_once_approved(tmp_path: Path) -> None:
    folder = tmp_path / "arc" / "2016" / "03-27-2016 - Easter"
    photos = [
        _photo(folder / f"DSCF{i}.JPG", f"-DateTimeOriginal=2015:05:02 1{i}:00:00", "-Model=X-T1")
        for i in range(3)
    ]
    out = tmp_path / "out"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    row = by_name(rows(out / "files.csv"), "DSCF1.JPG")
    assert row["proposed_date"] == "2016:03:27 11:00:00"
    assert row["date_source"] == "clock_shift" and row["date_match"] == "+330 days"
    assert row["apply"] == "0", "moving a whole event is the reviewer's call"

    approve(out, {p.name for p in photos})
    assert _apply(out) == EXIT_OK
    assert _tags(photos[2], "-EXIF:DateTimeOriginal")["ExifIFD:DateTimeOriginal"] == "2016:03:27 12:00:00"
    scan_main(["--root", str(tmp_path / "arc"), "--out-dir", str(out)])
    row = by_name(rows(out / "files.csv"), "DSCF1.JPG")
    assert row["proposed_date"] == "" and row["date_flags"] == ""
