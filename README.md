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

Open `out/folders.csv` first. It has one row per folder and is much shorter
than `files.csv`. Its `title` and `keywords` are what every photo in that folder
(and in folders below it without a title of their own) will get. Fix any titles
that came out wrong, and set `skip` to `1` for folders whose names should not
become titles. An inner folder like `Day 1` is titled with its parent's name,
`Disney Day 1`. `date_found` is for information only: dates are decided per
photo in `files.csv`, so that is where to change or reject one.

Then open `out/files.csv`. The column you edit is `apply`, at the far right.
Everything to its left is context:

| Column | What it tells you |
| --- | --- |
| `date_status` | `ok`, `missing`, `implausible`, or `clustered` |
| `date_flags` | why, e.g. `junk_default`, `future`, `burst_suspect`, `clock_shift`, `clustered_review`, `year_only`, `month_only`, `season_only`, `ambiguous_day_month`, `download_time`, `folder_disagrees`, `multi_year`, `coarser_than_existing`, `no_candidate`, `proposal_matches_existing`, `exiftool_error`, `mime_mismatch` |
| `existing_date` | the photo's current capture date: EXIF `DateTimeOriginal`, or failing that an XMP or IPTC creation date. A date with no time (`2015:06:20`) counts as a real date |
| `proposed_date` | what would be written. Blank means the date is fine and will not be touched, or that no trustworthy date was found. You can edit it |
| `date_source` | `existing_metadata`, `filename`, `folder`, `exif_createdate` (see the priority order below), or `clock_shift`. Filesystem created/modified dates are never used |
| `date_rule` | the specific rule that matched, for auditing a surprising result |
| `date_match` | the exact text the date was read from |
| `confidence` | `high` (another date already in the photo, the filename, or a CreateDate that agrees with the folder) or `medium` (a folder, or a CreateDate with nothing to check it against) |
| `cluster_size` / `cluster_folders` | how many files share this timestamp, and across how many folders. One folder can mean a camera burst, but also a batch edit or a set of scans |
| `existing_title` / `existing_keywords` | what is already there. These are kept: a filename or folder title never replaces them, only a value you type into `override_title` / `override_keywords` does. Camera boilerplate like `OLYMPUS DIGITAL CAMERA` does not count as a title. Apply checks the photo's real metadata in `scan.db`, not these cells, so editing or deleting them cannot unlock a title |
| `inherit_title` / `inherit_keywords` | what this file would inherit from its folder, with numeric dates stripped (`12.21 Barney on Christmas Eve` becomes `Barney on Christmas Eve`) |
| `filename_title` | a title taken from the filename when it is a real description (`Barney opening presents.jpg`). Blank for generic names like `IMG_4471`, `20205-01`, or `Canon.jpg`. When filled, it is used instead of the folder title; clear it to use the folder title |
| `title_to_write` / `keywords_to_write` | what an approved row would write, worked out when the scan ran. Blank means nothing (the photo already has a title or keywords). Edits to `folders.csv` or the override columns change it; apply recomputes it |
| `writable` | `0` means the format cannot store anything; those rows are never approved |

`override_title` and `override_keywords` are blank on purpose. Fill one in to
override both the filename and the folder for that one file, even replacing a
title or keywords the photo already has.

The title written is the first non-blank of `override_title`, `filename_title`,
and the folder's title. Keywords always come from `override_keywords` or the folder.
Titles lose their dates (`12.21`, `Dec 21`, `2021`) but keep punctuation that
belongs to the words, so `Mom & Dad 2019` becomes `Mom & Dad`, `U.S. trip`
becomes `U.S. Trip`, and `1/2 Marathon` stays. `July 4th 2019` becomes `4th of
July`. Folder names that say nothing once the year is gone (`Misc 2019`,
`Photos from 2015`, `Backup 2012`, `Takeout`, `Favorites`, `Lightroom Exports`,
`Snapchat Memories`, `100_PANA`) give no title. Hyphenated words and phrases
stay whole in keywords (`Mother-in-Law`, `St. Louis`, `Christmas Eve`), and
lowercase place acronyms are capitalized (`nyc trip` becomes `NYC Trip`). Derived titles
are title-cased (`barney on christmas eve` becomes `Barney on Christmas Eve`);
words that already carry capitals, like `iPhone` or `BBQ`, are left as written,
and a SHOUTED name is lowered except for known acronyms (`4TH OF JULY BBQ`
becomes `4th of July BBQ`).

IPTC limits a title and each keyword to 64 bytes. Longer values are cut at a
word boundary in the IPTC copy only; the XMP copy keeps the full text.

Two flags are worth understanding before you bulk-approve anything:

- **`no_candidate`** means the date is missing or bad and neither the EXIF data,
  the filename, nor any parent folder offered a replacement. The date is left
  alone. Filesystem created/modified dates are deliberately not used, because they
  usually record when the file was last copied rather than when it was taken.
- **`year_only`** means the date came from a folder named just a year, so it
  resolves to January 1st (January 2nd for 1970, 1980, and 1904, whose New
  Year's Day is a camera clock reset). It will display in Apple Photos as a
  specific day despite being accurate only to the year. **`month_only`** is the
  same for a folder like `2019-07` or `March 2019`, which resolves to the 1st of
  the month. **`season_only`** is the same for `Summer 2008`: seasons land
  mid-season (Winter January 15th, Spring April 15th, Summer July 15th,
  Fall/Autumn October 15th).
- **`ambiguous_day_month`** means a dotted date like `04.07.2003` could be
  April 7th or July 4th. Month-first is proposed, but the row is not approved
  by default.
- **`clustered_review`** means the photo's date is shared by many files and
  the replacement is only a folder date or CreateDate. A shared date can be a
  real event, so these are not approved by default.
- **`download_time`** means the date came from a number like
  `FB_IMG_1563012345678`, which records when the file was saved, not taken.
  It is only used when it falls inside the folder's date.
- **`folder_disagrees`** means the photo has a believable date that falls
  outside its folder's date, such as a camera clock left at 2001 in
  `Hawaii 2014`. Nothing is proposed; type a date into `proposed_date` if the
  folder is right.
- **`clock_shift`** means a camera's clock was plainly wrong. Every photo
  from that camera in a folder naming an exact day (`03-27-2016 - Easter`) is
  more than a month away from that day, and they were all taken within a few
  days of each other. They are moved by whole days (`date_match` says how
  many), which keeps their order and time of day. A shift is only proposed
  when the folders above agree with that day, so `2018/04-20-2019` proposes
  nothing. Not approved by default: filter `date_flags` for `clock_shift`,
  check a few against the folder, and set `apply` to `1`.
- **`exiftool_error`** means exiftool could not open the file during the scan
  (it is also listed in `scan_errors.csv`). Its existing metadata is unknown,
  so nothing is proposed and apply refuses the row even if you approve it. Fix
  the problem (often permissions) and rescan; the file is retried automatically.
- **`multi_year`** means a folder above the photo spans years (`Grandpa
  (1920-1995)`, `2019-2020 School Year`, `1985 Reunion 2015`), so no year can
  be picked from it, and folders above it are not consulted either.
- **`coarser_than_existing`** means the photo's clustered date falls inside
  the folder's year, month, or day, so the replacement would only throw away
  the time of day. Not approved by default.

When a date is missing or bad, a replacement is looked for in this order:

1. another date the photo already carries: when EXIF holds a scanner default but
   XMP or IPTC holds a real date, the real one is copied over
2. the filename: names devices and apps give (`IMG_20190704_153012`,
   `WhatsApp Image 2023-07-04 at 3.05.12 PM`), and dates people type into a
   name (`Mom and Dad 12-25-1975`, `Christmas 1975`). When the filename gives
   only a year or month, a narrower folder date or a CreateDate inside it is
   used instead
3. the nearest folder with a date. Folders whose date is when they were made
   (`takeout-2020…`, `Backup Jan 2012`, `iCloud Export 2021`) are ignored
4. EXIF `CreateDate`, only when it falls inside the folder's year, month, or day
   (it then adds the time of day), or when nothing else offers a date. On a
   scanned print it records the day of the scan, so it never overrides a
   folder that disagrees with it

Folder dates are more than years. A month and day in a folder name (`12.21`,
`12-25-2021`, `Dec 21`) or a holiday (`Christmas Eve`, `Thanksgiving`,
`Easter`, `Mother's Day`, `4th of July`, and others) is combined with a year
from the same folder or the nearest parent folder. So `2021 Christmas Day`,
`2021/12.21 Barney`, and `Christmas/2021` all give real days instead of
January 1st, and `date_rule` names the rule used (e.g. `christmas`,
`month_day+folder_year`). Numeric month-days are read month-first: `12.21` is
December 21st, and `05.06` is May 6th. `21 Dec` and `25th of December` work
too. Without a year, only two-digit pairs at the start of the name count, so
`1/2 Marathon`, `3.5 Mile Hike`, `6-8 Grade`, and `Game 3-2` are not dates;
with one, `Christmas 12-25-98` is December 25th 1998 wherever it appears.
`Christmas '98`, `Xmas 99`, `NYE 2009`, and `2015-3-14` work too. Ages
(`2.5 Years Old`), decades (`1990s`), quarters (`Jan-Mar 2020`), and people
(`Aunt May 2015`) are not read as months or days, and a sports or school season
like `Hockey 2010-11` is read as spanning two years.

A few notes on editing:

- Dates must be year-first: `YYYY:MM:DD HH:MM:SS`. `7/4/2019` is refused rather
  than guessed, because it means two different days in different locales and the
  CSV does not record which one wrote it.
- Keywords are separated by semicolons, so that values like `Portland, OR`
  survive.
- Save as UTF-8 CSV. In Excel that is **CSV UTF-8 (Comma delimited)**, not
  plain "CSV", which would mangle accented names; apply refuses such a file and
  says how to re-save it. Numbers exports UTF-8 by default.
- Rescanning regenerates both CSVs. If you edited one since the last scan, the
  edited copy is kept beside it as `files.edited-<date>-<time>.csv` so you can
  copy your decisions back.

### 3. Apply

Dry run first. It does everything except invoke exiftool, and puts what would
be written in `out/applied.dryrun.csv`:

```bash
just apply out --dry-run
```

It also warns about approved values a file cannot store: a non-Latin title on
a file whose old IPTC is Latin-1 under `--no-xmp`, say, or anything on a GIF
under `--no-xmp`.

Then for real:

```bash
just apply out
```

A new date is written to every date tag the photo already has (EXIF, XMP,
and IPTC), so no stale copy is left for Apple Photos to prefer, and the old
time zone offset and sub-seconds are removed along with it. A title or keywords
that a file's old Latin-1 IPTC cannot hold go to XMP, and the old IPTC copy is
removed rather than left disagreeing. Finder tags and other extended
attributes are copied onto the rewritten file.

Every approved file is re-checked against the scan snapshot before anything is
written. If a file changed since the scan, it is not written blind. Files that
fail the check are sorted into buckets in `apply_conflicts.csv`:

- `already_applied` — it already carries what this row intended, so the run is
  treated as a resumption and the file is skipped
- `modified_elsewhere` — it changed into something we did not intend, so it is
  refused
- `unwritable` — it is locked or read-only, its name is too long for
  exiftool's temporary copy, or a temporary file from an interrupted run is in
  the way, so it is refused

This is what makes `apply` safe to re-run. It matters because exiftool itself
will not stop a second write. `applied.csv` accumulates across runs, one row per
file, so after an interrupted run and a resumed one, verify still checks both.

Useful flags:

- `--merge-keywords` adds folder keywords alongside a photo's existing ones, skipping any it already has, and writes the combined list to both IPTC and XMP (without it, photos that already have keywords keep them untouched)
- `--no-xmp` writes EXIF and IPTC only
- `--force-conflicts` proceeds with the still-clean files when some are refused

### 4. Verify

```bash
just verify ~/Pictures/Archive out
```

Re-reads every written file and sorts each into `verified`, `not_applied`, or
`unexpected_change`. The third bucket is the valuable one: it compares against
the pre-apply snapshot in `scan.db` and reports any date, title, or keyword tag
that changed which `applied.csv` never asked for. Other metadata (camera make,
orientation, captions) is not compared. Apply saves that snapshot itself, so a
rescan after apply does not erase it; `scan.py --fresh` does. Keywords must
match exactly: a missing keyword or an extra one is reported.

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

**Some rows with bad dates get no date fix.** Sort by `date_flags` and look
for `no_candidate`: nothing trustworthy was found to replace the date, so
`proposed_date` is blank (the row may still be approved to add a title). You
can type a date into `proposed_date` and set `apply` to `1`.

**`scan.py` seems to hang.** It prints progress every five seconds to stderr.
If you piped stderr somewhere, you will not see it. The scan is resumable, so
interrupting it is not destructive.

**A folder of 40 photos all share one timestamp.** Check `cluster_folders`. If
it is `1`, it may be a camera burst with a correct timestamp, or a batch edit
or a set of scans that all got the same one; look at a few. These rows are
flagged `burst_suspect` and are not approved by default, even though a
replacement date is shown.

**A rescan after apply shows many `clustered` rows.** Photos dated from the same
folder share a timestamp, so they cluster. Where the proposal is the date the
photo already has, `proposed_date` is blank and the row is flagged
`proposal_matches_existing`: there is nothing to fix.

**Files with `date_status=ok` have a blank `proposed_date`.** Intended. Blank
means "leave this alone," which is both the honest thing to show you and the
correct instruction to exiftool.

## Scope

Processes `.jpg .jpeg .png .heic .heif .tif .tiff .gif .bmp .webp .dng .cr2
.nef .arw .raf .orf`. Skips and counts video. Skips dotfiles, AppleDouble
sidecars (`._name`), `Thumbs.db`, `__MACOSX`, symlinked files, NAS thumbnail
and recycle folders (`@eaDir`, `#recycle`), Lightroom preview folders
(`.lrdata`), and anything inside a Photos, iPhoto, or Aperture library. Files
deleted or moved since the last scan drop out of it on the next full rescan.

Not all formats can store all tags. Verified against exiftool 13.55–13.59:

| Format | EXIF | IPTC | XMP |
| --- | --- | --- | --- |
| jpg, tiff, raw | yes | yes | yes |
| heic, png, webp | yes | no | yes |
| gif | no | no | yes |
| bmp | no | no | no |

The tool writes only what a format accepts, so a `.gif` gets its date via
`XMP:DateCreated` and a `.bmp` is never approved at all.

IPTC text is marked UTF-8 only on files with no IPTC yet, or whose IPTC is
already UTF-8. Older files often hold Latin-1 IPTC (e.g. a caption with
`Café`); marking those UTF-8 would garble the existing text, so they keep their
encoding, and a value Latin-1 cannot hold goes to XMP only.

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
