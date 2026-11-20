"""Parse dates out of filenames and folder names.

The highest-risk module in the tool, so it is pure (no I/O), rule-ordered rather
than leftmost-match, and exhaustively tested against both positive and negative
tables. A wrong answer here writes a wrong capture time into a photo.

Two fencing strategies are in play and the difference matters:

* Most numeric rules fence on *digit* boundaries — `(?<!\\d)` / `(?!\\d)` — so a
  pattern cannot match inside a longer run of digits.
* The epoch rules fence on *token* boundaries — `(?<![0-9A-Za-z])` / `(?![0-9A-Za-z])`
  — because the spec says an epoch counts only "as a whole token". Digit fencing
  alone would let `IMG1234567890.jpg` match, since `G` is not a digit.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from typing import Final

from .models import DateCandidate

MIN_YEAR: Final = 1900
# Epochs get a tighter floor than the general sanity range: a 10-digit value is
# only plausible as a timestamp for the era in which digital photos exist.
EPOCH_MIN = datetime(1990, 1, 1)

#: Date-only values land at noon, never midnight. Midnight slides across the day
#: boundary under any timezone interpretation; noon cannot.
NOON: Final = (12, 0, 0)


# ---------------------------------------------------------------------------
# Guard 4 — camera sequence short circuit
# ---------------------------------------------------------------------------
# Note the asymmetry: the alpha prefixes make the separator optional, the numeric
# prefix REQUIRES one. Collapsing these into a single alternation (as an earlier
# draft did) is wrong in the worst possible direction — `\d{3}` would take "201"
# out of "20190704" and `\d{1,7}` would take "90704", rejecting a mandated
# positive. Same for "19990101" and the 10-digit epoch "1234567890".
#
# `\d{1,7}` and not `\d{1,8}`: an 8-digit run is exactly what YYYYMMDD looks
# like, so widening this by one would make the guard swallow real dates such as
# `100_20190704.jpg`. Seven is the longest run that cannot collide with a date.
#
# Alternatives are ordered longest-first (DSCN before DSC). Python's `re` would
# backtrack into the longer one anyway; the ordering makes the intent legible.
CAMERA_SEQ: Final = re.compile(
    r"^(?:"
    r"(?:DSCN|DSCF|DSC|IMGP|PICT|SAM|P)[-_]?\d{1,7}"  # alpha prefix, separator optional
    r"|\d{3}[-_]\d{1,7}"  # numeric prefix, separator REQUIRED
    r")$",
    re.IGNORECASE,
)


def _build(
    year: int,
    month: int,
    day: int,
    hour: int | None = None,
    minute: int | None = None,
    second: int | None = None,
    *,
    now: datetime,
) -> tuple[datetime, bool] | None:
    """Construct a sanity-checked datetime, or None if the fields are nonsense.

    Calendar validity is enforced by actually constructing the datetime rather
    than by range-checking each field, so 2019-13-40 and 2019-02-30 both fail.
    """
    if not (MIN_YEAR <= year <= now.year):
        return None
    has_time = hour is not None
    if not has_time:
        hour, minute, second = NOON
    assert hour is not None and minute is not None and second is not None
    if hour > 23 or minute > 59 or second > 59:
        # A bad time component drops the time rather than the whole candidate;
        # the date part is still evidence.
        hour, minute, second = NOON
        has_time = False
    try:
        dt = datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None
    if dt > now:
        return None
    return dt, has_time


def _from_epoch(value: int, *, now: datetime, millis: bool) -> tuple[datetime, bool] | None:
    seconds = value / 1000.0 if millis else float(value)
    try:
        dt = datetime.fromtimestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None
    if dt < EPOCH_MIN or dt > now:
        return None
    return dt, True


# ---------------------------------------------------------------------------
# Rules, most specific first. First match wins — this is deliberately not
# leftmost-match, because `PXL_20220101_123456789` must be read by the pixel rule
# rather than having ymd_compact grab a prefix of it.
# ---------------------------------------------------------------------------

_Rule = tuple[str, re.Pattern[str], Callable[[re.Match[str], datetime], tuple[datetime, bool] | None]]


def _pixel(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"]), now=now)


def _whatsapp(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), now=now)


def _screenshot_mac(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    hour = int(m["h"]) % 12
    if m["ampm"].upper().startswith("P"):
        hour += 12
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), hour, int(m["mi"]), int(m["s"]), now=now)


def _ymd(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    groups = m.groupdict()
    if groups.get("h") is not None:
        return _build(int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"]), now=now)
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), now=now)


def _epoch_ms(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _from_epoch(int(m["v"]), now=now, millis=True)


def _epoch_s(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _from_epoch(int(m["v"]), now=now, millis=False)


def _bare_year(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _build(int(m["y"]), 1, 1, now=now)


# macOS screenshots have two spellings across OS versions ("Screen Shot" through
# Mojave, "Screenshot" from Catalina), and Ventura onward inserts U+202F NARROW
# NO-BREAK SPACE before AM/PM. A pattern written with a literal space silently
# fails on every recent screenshot, so the meridiem separator accepts the whole
# family of spaces.
_MERIDIEM_SPACE = r"[\s\u202f\u00a0]*"

_RULES: Final[list[_Rule]] = [
    (
        "pixel",
        re.compile(
            r"PXL_(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})_(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})\d{3}",
            re.IGNORECASE,
        ),
        _pixel,
    ),
    (
        "whatsapp",
        re.compile(
            r"(?:IMG|VID)-(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})-WA\d{4}",
            re.IGNORECASE,
        ),
        _whatsapp,
    ),
    (
        "screenshot_mac",
        re.compile(
            r"Screen\s?shot[ _]"
            r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})"
            r"[ _]at[ _]"
            r"(?P<h>\d{1,2})\.(?P<mi>\d{2})\.(?P<s>\d{2})"
            rf"{_MERIDIEM_SPACE}(?P<ampm>[AP]\.?M\.?)",
            re.IGNORECASE,
        ),
        _screenshot_mac,
    ),
    (
        "screenshot_android",
        re.compile(
            r"Screenshot[_-](?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"
            r"[-_](?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})",
            re.IGNORECASE,
        ),
        _ymd,
    ),
    (
        "ymd_sep",
        re.compile(
            r"(?<!\d)(?P<y>\d{4})[-_.](?P<mo>\d{2})[-_.](?P<d>\d{2})"
            r"(?:[ _T-]+(?P<h>\d{2})[-_.:](?P<mi>\d{2})[-_.:](?P<s>\d{2}))?"
            r"(?!\d)"
        ),
        _ymd,
    ),
    (
        "ymd_compact",
        re.compile(
            r"(?<!\d)(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"
            r"(?:[ _T-]?(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2}))?"
            r"(?!\d)"
        ),
        _ymd,
    ),
    # Token-boundary fenced, per the spec's "whole token" wording.
    (
        "epoch_ms",
        re.compile(r"(?<![0-9A-Za-z])(?P<v>\d{13})(?![0-9A-Za-z])"),
        _epoch_ms,
    ),
    (
        "epoch_s",
        re.compile(r"(?<![0-9A-Za-z])(?P<v>\d{10})(?![0-9A-Za-z])"),
        _epoch_s,
    ),
]

#: Bare years are meaningful in a folder name and catastrophic in a filename,
#: where they are almost always a camera sequence number. This rule is appended
#: only for `parse_folder_date`.
_BARE_YEAR: Final[_Rule] = (
    "bare_year",
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})(?!\d)"),
    _bare_year,
)


def _apply_rules(text: str, rules: list[_Rule], *, now: datetime) -> DateCandidate | None:
    for name, pattern, builder in rules:
        for match in pattern.finditer(text):
            built = builder(match, now)
            if built is not None:
                dt, has_time = built
                return DateCandidate(dt=dt, match=match.group(0), rule=name, has_time=has_time)
    return None


def strip_extension(filename: str) -> str:
    """Drop a trailing extension so `IMG_2019.jpg` cannot borrow digits from it."""
    stem, dot, ext = filename.rpartition(".")
    if dot and 1 <= len(ext) <= 5 and not ext.isdigit():
        return stem
    return filename


def is_camera_sequence(stem: str) -> bool:
    """Guard 4: a bare camera sequence number that merely looks date-shaped."""
    return CAMERA_SEQ.match(stem) is not None


def parse_filename_date(filename: str, *, now: datetime | None = None) -> DateCandidate | None:
    """Parse a date from a filename, or None.

    Four overlapping guards make the mandated negatives fail. They are
    deliberately redundant: `IMG_2019.jpg` is caught by the absence of a
    bare-year rule rather than by the camera-sequence short circuit, and
    `DSC_0704 (1).jpg` escapes the short circuit's `$` anchor entirely.
    """
    now = now or datetime.now()
    stem = strip_extension(filename)
    if is_camera_sequence(stem):
        return None
    return _apply_rules(stem, _RULES, now=now)


def parse_folder_date(name: str, *, now: datetime | None = None) -> DateCandidate | None:
    """Parse a date from a folder name. Unlike filenames, a bare year counts."""
    now = now or datetime.now()
    return _apply_rules(name, [*_RULES, _BARE_YEAR], now=now)
