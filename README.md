# photofix

Audit and repair photo metadata in a large archive, in preparation for importing
into Apple Photos.

The tool reads your archive, proposes fixes in a spreadsheet, waits for you to
approve them, and only then writes. It never moves, renames, or deletes
anything, and every file it writes keeps a pristine `_original` copy beside it.

## How it works

Three phases, with files on disk as the boundaries between them. Each phase is
independently re-runnable and resumable.

| Phase | Command | Reads | Writes |
| --- | --- | --- | --- |
| 1 | `scan.py` | your archive | `files.csv`, `folders.csv`, `scan_errors.csv`, `scan.db` |
| 2 | `apply.py` | the reviewed CSVs | your photos, `applied.csv`, `apply_conflicts.csv` |
| 3 | `verify.py` | your archive, `applied.csv` | `verify_report.csv`, `originals.csv` |

Phase 2 is the only one that modifies an image, and it writes only rows where
you set `apply` to `1`.

## Setup on macOS

```bash
brew install exiftool uv just
git clone <this repo> && cd photofix
uv sync
just probe      # confirm exiftool behaves as the tool expects
just check      # lint, types, tests
```

`just probe` is worth running once on any new machine. It verifies the exiftool
behaviors the design depends on — backup naming, that empty CSV cells mean
"leave this alone", that accented values survive a write/read round trip, and
which tag families each image format will actually accept.

## Running it

### 1. Scan

```bash
just scan ~/Pictures/Archive
```

Nothing is written to your photos. This produces `out/files.csv` and
`out/folders.csv`.

On a large archive this takes a while. It prints progress every five seconds and
is resumable: if you interrupt it, running the same command again picks up where
it stopped rather than starting over. Use `--fresh` to deliberately start over.

Rehearse on a subset first:

```bash
uv run python scan.py --root ~/Pictures/Archive --out-dir out --limit 200
```

Under `--limit`, treat `date_status=clustered` as unreliable — clustering
compares each file against every other one, and a partial scan has not seen the
rest yet.

### 2. Review

Open `out/folders.csv` first. It is much shorter than `files.csv` and it drives
the titles and keywords that most photos will inherit. Fix any folder titles
that came out wrong, and set `skip` to `1` for folders whose names should not
become titles.

Then open `out/files.csv`. The column you edit is `apply`, at the far right.
Everything to its left is context:

| Column | What it tells you |
| --- | --- |
| `date_status` | `ok`, `missing`, `implausible`, or `clustered` |
| `date_flags` | why, e.g. `junk_default`, `future`, `burst_suspect`, `year_only`, `fs_only`, `mime_mismatch` |
| `proposed_date` | what would be written. Blank means the date is fine and will not be touched |
| `date_source` | `exif_createdate`, `filename`, `folder`, or `filesystem` |
| `date_rule` | the specific rule that matched, for auditing a surprising result |
| `date_match` | the exact text the date was read from |
| `confidence` | `high`, `medium`, `low`. Rows default to `apply=1` at high and medium only |
| `cluster_size` / `cluster_folders` | how many files share this timestamp, and across how many folders. One folder usually means a camera burst, not damage |
| `existing_title` / `existing_keywords` | what is already there, so you can see what a write would replace |
| `inherit_title` / `inherit_keywords` | what this file would inherit from its folder |
| `writable` | `0` means the format cannot store anything; those rows are never approved |

`proposed_title` and `proposed_keywords` are blank on purpose. They are override
slots: fill one in to override what the folder would supply for that one file.

Two flags are worth understanding before you bulk-approve anything:

- **`fs_only`** means the only date we could find is a filesystem timestamp,
  which usually records when the file was last copied between disks rather than
  when the photo was taken. These rows default to `apply=0` even when the folder
  offers a good title, because `apply` approves the whole row — auto-approving
  them to get the title would also stamp the guessed date onto every file.
  If you want the title but not the date, clear `proposed_date` and set
  `apply` to `1`.
- **`year_only`** means the date came from a folder named just a year, so it
  resolves to January 1st. It will display in Apple Photos as a specific day
  despite being accurate only to the year.

A few notes on editing:

- Dates must be year-first: `YYYY:MM:DD HH:MM:SS`. `7/4/2019` is refused rather
  than guessed, because it means two different days in different locales and the
  CSV does not record which one wrote it.
- Keywords are separated by semicolons, so that values like `Portland, OR`
  survive.
- Save as CSV in the same format. Excel and Numbers both round-trip correctly.

### 3. Apply

Dry run first. It does everything except invoke exiftool:

```bash
just apply out --dry-run
```

Then for real:

```bash
just apply out
```

Every approved file is re-checked against the scan snapshot before anything is
written. If a file changed since the scan, it is not written blind — it is
sorted into one of two buckets in `apply_conflicts.csv`:

- `already_applied` — it already carries what this row intended, so the run is
  treated as a resumption and the file is skipped
- `modified_elsewhere` — it changed into something we did not intend, so it is
  refused

This is what makes `apply` safe to re-run. It matters because exiftool itself
will not stop a second write.

Useful flags:

- `--merge-keywords` adds to existing keywords instead of replacing them
- `--no-xmp` writes EXIF and IPTC only
- `--force-conflicts` proceeds with the still-clean files when some are refused

### 4. Verify

```bash
just verify ~/Pictures/Archive out
```

Re-reads every written file and sorts each into `verified`, `not_applied`, or
`unexpected_change`. The third bucket is the valuable one: it compares against
the pre-apply snapshot in `scan.db` and reports anything that changed which
`applied.csv` never asked for.

Exits nonzero if anything failed.

### 5. Clean up

Once you are satisfied, the `_original` backups can go. They are listed in
`out/originals.csv`. The tool will not delete them for you — they are your only
undo path.

```bash
# only after you have verified the results
find ~/Pictures/Archive -name '*_original' -delete
```

## Things that look like failures but are not

**The second `apply` run says "nothing to do" and writes zero files.** That is
success, and it exits `0`. Every row was recognized as already carrying the
intended values. Check `apply_conflicts.csv` — they will all be classified
`already_applied`.

**Far fewer rows are approved by default than you expected.** Only high- and
medium-confidence dates auto-approve. Sort by `date_flags` and look for
`fs_only`: those are files where nothing but the filesystem timestamp was
available, and they are waiting for you to decide.

**`scan.py` seems to hang.** It prints progress every five seconds to stderr.
If you piped stderr somewhere, you will not see it. The scan is resumable, so
interrupting it is not destructive.

**A folder of 40 photos all share one timestamp.** Check `cluster_folders`. If
it is `1`, that is a camera burst and the timestamp is correct.

**Files with `date_status=ok` have a blank `proposed_date`.** Intended. Blank
means "leave this alone," which is both the honest thing to show you and the
correct instruction to exiftool.

## Scope

Processes `.jpg .jpeg .png .heic .heif .tif .tiff .gif .bmp .webp .dng .cr2
.nef .arw .raf .orf`. Skips and counts video. Skips dotfiles, AppleDouble
sidecars (`._name`), `Thumbs.db`, `__MACOSX`, and anything inside a
`.photoslibrary`.

Not all formats can store all tags. Verified against exiftool 13.59:

| Format | EXIF | IPTC | XMP |
| --- | --- | --- | --- |
| jpg, tiff, heic, raw | yes | yes | yes |
| png, webp | yes | no | yes |
| gif | no | no | yes |
| bmp | no | no | no |

The tool writes only what a format accepts, so a `.gif` gets its date via
`XMP:DateCreated` and a `.bmp` is never approved at all.

## Non-goals

No moving, renaming, or deleting. No video metadata. No GPS, face, or duplicate
detection. No timezone conversion — dates are written as wall-clock times in the
form exiftool expects, which is how cameras record them. No third-party imaging
libraries; all metadata I/O goes through exiftool.

## Development

```bash
just check    # preflight + lint + types + tests
just test     # tests only
just probe    # re-verify exiftool behaviors
```

Tests marked `exiftool` need the binary. `just check` runs `preflight` first so
a missing binary fails the gate rather than silently skipping the integration
suite.
