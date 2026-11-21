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
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Final

from .models import DateCandidate

MIN_YEAR: Final = 1900
# Epochs get a tighter floor than the general sanity range: a 10-digit value is
# only plausible as a timestamp for the era in which digital photos exist.
EPOCH_MIN = datetime(1990, 1, 1)

#: Date-only values land at noon, never midnight. Midnight slides across the day
#: boundary under any timezone interpretation; noon cannot.
NOON: Final = (12, 0, 0)

#: An age, a duration, or a measure follows, so the number is not a date:
#: "2.5 Years Old", "18-24 months", "Spring 10K".
NOT_A_DATE_AFTER = (
    r"(?!\s*(?:years?|yrs?|months?|mos?|weeks?|wks?|days?|old|y/?o"
    r"|grade|graders?|kids|miles?|mi|km|k|lbs?|pounds|points|pts|inch(?:es)?|ft|feet|hours?|hrs?)\b)"
)


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
    """Pixel names are UTC: PXL_20230101_030000 is the evening of Dec 31 in
    California. Converted with this computer's timezone, the best guess there is."""
    built = _build(int(m["y"]), int(m["mo"]), int(m["d"]), int(m["h"]), int(m["mi"]), int(m["s"]), now=now)
    if built is None:
        return None
    local = built[0].replace(tzinfo=UTC).astimezone().replace(tzinfo=None)
    return (local, True) if local <= now else None


def _whatsapp(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), now=now)


def _screenshot_mac(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    hour = int(m["h"]) % 12
    if m["ampm"].upper().startswith("P"):
        hour += 12
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), hour, int(m["mi"]), int(m["s"]), now=now)


def _ymd(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    groups = m.groupdict()
    for h, mi, s in (("h", "mi", "s"), ("h2", "mi2", "s2")):
        if groups.get(h) is not None:
            hour = int(m[h])
            if groups.get("ampm"):
                if not 1 <= hour <= 12:
                    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), now=now)
                hour = hour % 12 + (12 if m["ampm"].upper().startswith("P") else 0)
            return _build(int(m["y"]), int(m["mo"]), int(m["d"]), hour, int(m[mi]), int(m[s]), now=now)
    return _build(int(m["y"]), int(m["mo"]), int(m["d"]), now=now)


def _epoch_ms(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _from_epoch(int(m["v"]), now=now, millis=True)


def _epoch_s(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    return _from_epoch(int(m["v"]), now=now, millis=False)


#: Days a reset camera clock lands on. A folder named "1980" must not be given
#: one of them, or every rescan would call the date it was given junk.
CLOCK_RESET_DAYS: Final[frozenset[str]] = frozenset({"1904:01:01", "1970:01:01", "1980:01:01"})


def _first_day(year: int, month: int) -> int:
    return 2 if f"{year:04d}:{month:02d}:01" in CLOCK_RESET_DAYS else 1


def _bare_year(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    year = int(m["y"])
    return _build(year, 1, _first_day(year, 1), now=now)


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
            # "WhatsApp Image 2019-07-04 at 15.05.12", "... at 3.05.12 PM",
            # "Photo 2019-07-04 15 30 12", "signal-2019-07-04-150512"
            r"(?:[ _T-]+(?:at[ _]+)?"
            r"(?:(?P<h>\d{1,2})[-_.: ](?P<mi>\d{2})[-_.: ](?P<s>\d{2})"
            rf"(?:{_MERIDIEM_SPACE}(?P<ampm>[AP]\.?M\.?)(?![A-Za-z]))?"
            r"|(?P<h2>\d{2})(?P<mi2>\d{2})(?P<s2>\d{2})))?"
            r"(?!\d)",
            re.IGNORECASE,
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
    # Epochs record when a file was saved or downloaded ("FB_IMG_1563012345678"),
    # not when the photo was taken, and plenty of IDs look like one. The 13-digit
    # millisecond form is token-fenced; a 10-digit number counts only as the
    # whole name, since "Snapchat-1187654321" and print-order numbers are IDs.
    (
        "epoch_ms",
        re.compile(r"(?<![0-9A-Za-z])(?P<v>\d{13})(?![0-9A-Za-z])"),
        _epoch_ms,
    ),
    (
        "epoch_s",
        re.compile(r"^(?P<v>\d{10})(?:\s*\(\d+\))?$"),
        _epoch_s,
    ),
]

#: Rules whose date is a save or download time: trusted less than a folder.
EPOCH_RULES: Final[frozenset[str]] = frozenset({"epoch_ms", "epoch_s"})

#: Bare years are meaningful in a folder name and catastrophic in a filename,
#: where they are almost always a camera sequence number. This rule is appended
#: only for `parse_folder_date`.
_BARE_YEAR: Final[_Rule] = (
    "bare_year",
    # Not a decade: "1990s" is ten years, not January 1st 1990.
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})(?!\d|['\u2019]?s\b)"),
    _bare_year,
)


def _short_year(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    year = _year(m["yy"], now)
    return _build(year, 1, _first_day(year, 1), now=now) if year else None


#: "Christmas '98". Named bare_year because it is one, so a holiday refines it.
_APOSTROPHE_YEAR: Final[_Rule] = (
    "bare_year",
    re.compile(r"(?<![\w'\u2019])['\u2019](?P<yy>\d{2})(?![\d'\u2019]|s\b)"),
    _short_year,
)

#: Words after which two bare digits are a year: "Xmas 99", "Halloween 04".
#: Not "Birthday 40" or "Game 12": those are an age and a number.
_YEAR_WORDS = (
    r"christmas|xmas|halloween|easter|thanksgiving|hanukkah|chanukah|nye"
    r"|new\s+year['\u2019]?s(?:\s+eve|\s+day)?|spring|summer|fall|autumn|winter"
)
SHORT_YEAR_AFTER_WORD = re.compile(
    rf"\b(?:{_YEAR_WORDS})[\s_-]+(?P<yy>\d{{2}})(?![\d'\u2019]|\s*(?:s|th|st|nd|rd)\b)" + NOT_A_DATE_AFTER,
    re.IGNORECASE,
)
_SHORT_YEAR_RULE: Final[_Rule] = ("bare_year", SHORT_YEAR_AFTER_WORD, _short_year)

#: A season and a year: the middle of the season, not January 1st. Winter is
#: January, the month that carries the year's number in "Winter 2008".
_SEASON_MONTH: Final[dict[str, int]] = {"spring": 4, "summer": 7, "fall": 10, "autumn": 10, "winter": 1}
_SEASON_YEAR = r"(?:19|20)\d{2}|['\u2019]?\d{2}"


def _season(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    raw = m["y"].lstrip("'\u2019")
    year = int(raw) if len(raw) == 4 else _year(raw, now)
    if year is None:
        return None
    return _build(year, _SEASON_MONTH[m["season"].lower()], 15, now=now)


_SEASON_RULES: Final[list[_Rule]] = [
    (
        "season",
        re.compile(
            rf"\b(?P<season>spring|summer|fall|autumn|winter)[\s_-]+(?:of[\s_-]+)?"
            rf"(?P<y>{_SEASON_YEAR})(?![\d'\u2019-]|\s*(?:s|th|st|nd|rd)\b)" + NOT_A_DATE_AFTER,
            re.IGNORECASE,
        ),
        _season,
    ),
    (
        "season",
        re.compile(
            r"(?<!\d)(?P<y>(?:19|20)\d{2})[\s_-]+(?P<season>spring|summer|fall|autumn|winter)\b",
            re.IGNORECASE,
        ),
        _season,
    ),
]

#: Download names from Facebook ("20011223_10153412_n.jpg"): the digits are
#: photo and album IDs, not dates.
_FACEBOOK_ID = re.compile(r"^\d+_\d+(?:_\d+)?_[nobs]$")

_YEAR_TOKEN = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_SHORT_YEAR_RANGE = re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})\s*[-\u2013\u2014/]\s*(?P<yy>\d{2})(?!\d)")
#: Things that run from one year into the next, so "Hockey 2010-11" is a season
#: rather than November 2010.
_SEASON_CONTEXT = re.compile(
    r"\b(?:season|school|year|yr|academic|semester|term|class|grade|kindergarten|preschool|pre-?k"
    r"|college|university|freshman|sophomore|junior|senior|league|team|club|tournament"
    r"|hockey|soccer|basketball|baseball|football|softball|volleyball|lacrosse|swim(?:ming)?"
    r"|ski(?:ing)?|snowboard(?:ing)?|skating|gymnastics|wrestling|track|band|choir|scouts?|winter)\b",
    re.IGNORECASE,
)


def year_span(name: str) -> tuple[int, int] | None:
    """The first and last year when a name covers more than one, else None.

    "Grandpa (1920-1995)", "2019-2020 School Year", "Kindergarten 2019-20",
    "1985 Reunion 2015", "Hockey 2010-11". Any single year picked from these
    would be a guess, and January 1st of the first one is the worst guess of all.
    """
    years = {int(y) for y in _YEAR_TOKEN.findall(name)}
    for m in _SHORT_YEAR_RANGE.finditer(name):
        start, tail = int(m["y"]), int(m["yy"])
        # "2010-11" is November unless the name says it is a season; "2010-13"
        # cannot be a month at all.
        consecutive = tail == (start + 1) % 100 and _SEASON_CONTEXT.search(name) is not None
        if (tail > 12 and tail > start % 100) or consecutive:
            years.add(start - start % 100 + tail + (100 if tail < start % 100 else 0))
    return (min(years), max(years)) if len(years) > 1 else None


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
    if is_camera_sequence(stem) or _FACEBOOK_ID.match(stem):
        return None
    return _apply_rules(stem, _RULES, now=now)


#: Words that make a following "May", "April", or "June" a person: "Aunt May 2015".
_PERSON_BEFORE = re.compile(
    r"\b(?:aunt|auntie|aunty|uncle|cousin|grandma|grandpa|granny|nana|nanna|nan|great"
    r"|miss|mrs|ms|mr|sister|sis|baby)\W*$",
    re.IGNORECASE,
)


def month_is_a_name(text: str, start: int) -> bool:
    """Whether the month word at `start` follows a title that makes it a name."""
    return _PERSON_BEFORE.search(text[:start]) is not None


def _year_month(m: re.Match[str], now: datetime) -> tuple[datetime, bool] | None:
    if m["mo"].isdigit():
        month = int(m["mo"])
    else:
        before = m.string[: m.start("mo")]
        # "Jan-Mar 2020" is a quarter, not March 1st.
        if month_is_a_name(m.string, m.start("mo")) or _MONTH_RANGE_BEFORE.search(before):
            return None
        month = _MONTH_NUMBERS[m["mo"].lower()]
    year = int(m["y"])
    return _build(year, month, _first_day(year, month), now=now)


_MONTH_NAMES = "|".join(
    sorted(
        ["january", "february", "march", "april", "may", "june", "july", "august", "september"]
        + ["october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug"]
        + ["sep", "sept", "oct", "nov", "dec"],
        key=len,
        reverse=True,
    )
)
#: Public for titles, which strip a month standing beside a year.
MONTH_NAMES: Final = _MONTH_NAMES
_MONTH_RANGE_BEFORE = re.compile(
    rf"\b(?:{_MONTH_NAMES})\.?\s*(?:-|\u2013|\u2014|to|thru|through|&|and)\s*$", re.IGNORECASE
)

#: A month and a year with no day: "2019-07", "07-2019", "March 2019",
#: "2019 March". The 1st of the month, not January 1st. Folder names only.
_MONTH_YEAR_RULES: Final[list[_Rule]] = [
    (
        "year_month",
        re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.](?P<mo>\d{1,2})(?!\d|[-_./]\d)"),
        _year_month,
    ),
    (
        "year_month",
        re.compile(r"(?<![\d./-])(?P<mo>\d{1,2})[-_./](?P<y>(?:19|20)\d{2})(?!\d)"),
        _year_month,
    ),
    (
        "year_month",
        re.compile(rf"\b(?P<mo>{_MONTH_NAMES})\.?,?\s+(?P<y>(?:19|20)\d{{2}})(?!\d)", re.IGNORECASE),
        _year_month,
    ),
    (
        "year_month",
        re.compile(rf"(?<!\d)(?P<y>(?:19|20)\d{{2}})[\s_-]+(?P<mo>{_MONTH_NAMES})\b", re.IGNORECASE),
        _year_month,
    ),
]

#: Folder rules that know less than a day, so a month-day or holiday in the
#: same name ("2021 Christmas Day", "2021 Dec 21") refines them.
COARSE_FOLDER_RULES: Final[frozenset[str]] = frozenset({"bare_year", "year_month", "season"})


#: "2015-3-14": a folder typed by hand drops the leading zeros.
_YMD_LOOSE: Final[_Rule] = (
    "ymd_loose",
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-_.](?P<mo>\d{1,2})[-_.](?P<d>\d{1,2})(?!\d)"),
    _ymd,
)


def parse_folder_date(name: str, *, now: datetime | None = None) -> DateCandidate | None:
    """Parse a date from a folder name. Unlike filenames, a bare year counts.

    A name spanning several years gives nothing: see `year_span`.
    """
    now = now or datetime.now()
    if year_span(name):
        return None
    rules = [
        *_RULES,
        _YMD_LOOSE,
        *_MONTH_YEAR_RULES,
        *_SEASON_RULES,
        _BARE_YEAR,
        _APOSTROPHE_YEAR,
        _SHORT_YEAR_RULE,
    ]
    return _apply_rules(name, rules, now=now)


#: A bare year in a filename only when spaces or brackets set it apart, as a
#: person types it: "Christmas 1975", "1975 wedding". Never "IMG_2019".
_SPACED_YEAR: Final[_Rule] = (
    "bare_year",
    re.compile(r"(?:^|(?<=[\s(\[]))(?P<y>(?:19|20)\d{2})(?=$|[\s)\]])"),
    _bare_year,
)
#: Named months only: "June 2014", not "2014-06", which in a filename is as
#: likely a camera counter.
_NAMED_MONTH_YEAR_RULES: Final[list[_Rule]] = _MONTH_YEAR_RULES[2:]


def parse_written_date(
    filename: str, *, descriptive: bool, now: datetime | None = None
) -> DateCandidate | None:
    """A date a person typed into a filename that the device rules did not find.

    A full day ("Mom and Dad 12-25-1975", "14.06.1975 Hochzeit", "Dec 25, 1975")
    counts in any filename that is not a camera sequence. Anything coarser — a
    year, a month, a season, or a holiday with a year ("Christmas 1975") — only
    in a `descriptive` one, i.e. one that also yields a title: in "IMG 2019"
    the number is a counter.
    """
    now = now or datetime.now()
    stem = strip_extension(filename)
    if is_camera_sequence(stem) or _FACEBOOK_ID.match(stem) or year_span(stem):
        return None
    full = _apply_rules(stem, [_YMD_LOOSE], now=now)
    if full is not None:
        return full
    partial = parse_folder_partial(stem, now=now, year_required=not descriptive)
    if partial is not None and partial.year is not None:
        dt = partial.on(partial.year, now=now)
        if dt is not None:
            return DateCandidate(dt=dt, match=partial.match, rule=partial.rule, has_time=False)
    if not descriptive:
        return None
    coarse = _apply_rules(
        stem,
        [*_NAMED_MONTH_YEAR_RULES, *_SEASON_RULES, _SPACED_YEAR, _APOSTROPHE_YEAR, _SHORT_YEAR_RULE],
        now=now,
    )
    if coarse is not None and partial is not None and (dt := partial.on(coarse.dt.year, now=now)):
        # "Christmas 1975": the holiday names the day, the number the year.
        return DateCandidate(
            dt=dt, match=f"{partial.match} + {coarse.match}", rule=partial.rule, has_time=False
        )
    return coarse


# ---------------------------------------------------------------------------
# Partial dates: a month and day whose year comes from elsewhere
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PartialDate:
    """A day of the year from a folder name, e.g. "12.21" or "Christmas Day".

    `year` is set only when the same text carried one ("12-25-2021"). Otherwise
    the year comes from a bare year in the same folder or from a parent folder,
    which is why the day is a function of the year: Thanksgiving moves.
    """

    match: str
    rule: str
    resolve: Callable[[int], date]
    year: int | None = None

    def on(self, year: int, *, now: datetime) -> datetime | None:
        if year < MIN_YEAR:
            return None
        try:
            day = self.resolve(year)
        except ValueError:  # Feb 29 in a non-leap year
            return None
        dt = datetime(day.year, day.month, day.day, *NOON)
        return dt if dt <= now else None


def _fixed(month: int, day: int) -> Callable[[int], date]:
    return lambda year: date(year, month, day)


def _nth_weekday(month: int, weekday: int, n: int) -> Callable[[int], date]:
    """The nth `weekday` (Monday=0) of `month`; n=-1 is the last one."""

    def resolve(year: int) -> date:
        if n > 0:
            first = date(year, month, 1)
            return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
        last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
        return last - timedelta(days=(last.weekday() - weekday) % 7)

    return resolve


def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 22 * m) // 451
    month = (h + m - 7 * n + 114) // 31
    day = (h + m - 7 * n + 114) % 31 + 1
    return date(year, month, day)


_APOS = "['\u2019]?"

#: Most specific first: "Christmas Eve" must win over "Christmas". Names that
#: double as places or surnames ("Easter Island", "St Patrick's Cathedral",
#: "the Valentine family") need the qualifier that makes them a holiday.
_HOLIDAYS: Final[list[tuple[str, re.Pattern[str], Callable[[int], date]]]] = [
    (name, re.compile(rf"\b(?:{pattern})\b(?!\s+island)", re.IGNORECASE), resolve)
    for name, pattern, resolve in [
        ("christmas_eve", r"(?:christmas|xmas)\s+eve", _fixed(12, 24)),
        ("christmas", r"christmas|xmas", _fixed(12, 25)),
        ("new_years_eve", rf"new\s+year{_APOS}s?\s+eve|nye", _fixed(12, 31)),
        ("new_years_day", rf"new\s+year{_APOS}s?\s+day", _fixed(1, 1)),
        ("halloween", rf"hallowe{_APOS}en", _fixed(10, 31)),
        ("valentines_day", rf"valentine{_APOS}s(?:\s+day)?|valentine\s+day", _fixed(2, 14)),
        ("independence_day", r"(?:4th|fourth)\s+of\s+july|july\s+4th|independence\s+day", _fixed(7, 4)),
        ("st_patricks_day", rf"(?:st\.?|saint)\s+patrick{_APOS}s?\s+day", _fixed(3, 17)),
        ("cinco_de_mayo", r"cinco\s+de\s+mayo", _fixed(5, 5)),
        ("thanksgiving", r"thanksgiving", _nth_weekday(11, 3, 4)),
        ("easter", r"easter", _easter),
        ("mothers_day", rf"mother{_APOS}s?\s+day", _nth_weekday(5, 6, 2)),
        ("fathers_day", rf"father{_APOS}s?\s+day", _nth_weekday(6, 6, 3)),
        ("memorial_day", r"memorial\s+day", _nth_weekday(5, 0, -1)),
        ("labor_day", r"labou?r\s+day", _nth_weekday(9, 0, 1)),
    ]
]

#: Public for keywords, which keep a holiday's name whole ("Christmas Eve").
HOLIDAY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(pattern for _, pattern, _ in _HOLIDAYS)

_MONTH_NUMBERS: Final[dict[str, int]] = {
    name: number
    for number, names in enumerate(
        [
            ("january", "jan"),
            ("february", "feb"),
            ("march", "mar"),
            ("april", "apr"),
            ("may",),
            ("june", "jun"),
            ("july", "jul"),
            ("august", "aug"),
            ("september", "sep", "sept"),
            ("october", "oct"),
            ("november", "nov"),
            ("december", "dec"),
        ],
        start=1,
    )
    for name in names
}

#: "12.21", "12-25", "12/25/2021", "12.21.21". Fenced on separators as well as
#: digits so it cannot match the tail of a year-first date like 2021-12-25.
#: The separator must repeat: "12-25-98", not "12-25.98".
_NUMERIC_MONTH_DAY = re.compile(
    r"(?<![A-Za-z\d./-])(?P<a>\d{1,2})(?P<sep>[./-])(?P<b>\d{1,2})(?:(?P=sep)(?P<y>\d{4}|\d{2}))?"
    r"(?![\d./-])" + NOT_A_DATE_AFTER,
    re.IGNORECASE,
)
_LEADING_YEAR = re.compile(r"\s*(?:(?:19|20)\d{2}[\s_.-]+)?")
_DAY = r"(?:3[01]|[12]\d|0?[1-9])"
#: "Dec 21", "December 25th", "Dec. 21, 2021", "21 Dec", "25th of December".
#: Public because titles strip it too.
NAMED_MONTH_DAY = re.compile(
    rf"\b(?:(?P<mon>{_MONTH_NAMES})\.?\s+(?P<d>{_DAY})(?:st|nd|rd|th)?(?!\d)"
    rf"|(?<!\d)(?P<d2>{_DAY})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon2>{_MONTH_NAMES})\b\.?)"
    r"(?:,?\s+(?P<y>\d{4})(?!\d))?",
    re.IGNORECASE,
)


def is_named_date(m: re.Match[str]) -> bool:
    """Whether a NAMED_MONTH_DAY match is a date rather than "Aunt May 5th"."""
    return m["d2"] is not None or not month_is_a_name(m.string, m.start())


def _year(text: str | None, now: datetime) -> int | None:
    if not text:
        return None
    year = int(text)
    if year < 100:
        year += 2000 if 2000 + year <= now.year else 1900
    return year


def parse_folder_partial(
    name: str, *, now: datetime | None = None, year_required: bool = False
) -> PartialDate | None:
    """A month and day in a folder name, from numbers, a month name, or a holiday.

    Numeric forms are read month-first ("12.21" is December 21st), falling back
    to day-first only when month-first is impossible ("21.12"). Dotted dates
    that work both ways ("04.07.2003", usually 4 July outside the US) get the
    rule "month_day_ambiguous" so a reviewer has to choose. With
    `year_required`, only dates that carry their own year count.
    """
    now = now or datetime.now()

    for m in _NUMERIC_MONTH_DAY.finditer(name):
        if year_required and not m["y"]:
            continue
        # A month, day, and year ("Christmas 12-25-98") is a date anywhere.
        # Without the year, numbers are as likely a score, a fraction, a
        # distance, or a range ("Game 3-2", "1/2 Marathon", "3.5 Mile Hike",
        # "6-8 Grade"), so only two-digit pairs leading the name count,
        # optionally after a year: "12.21 Barney", "2021 12.21".
        if not m["y"]:
            leading = _LEADING_YEAR.fullmatch(name[: m.start()]) is not None
            if not (leading and len(m["a"]) == 2 and len(m["b"]) == 2):
                continue
        month, day = int(m["a"]), int(m["b"])
        if month > 12 and day <= 12:
            month, day = day, month
        if 1 <= month <= 12 and 1 <= day <= 31:
            ambiguous = m["sep"] == "." and month <= 12 and day <= 12 and month != day
            rule = "month_day_ambiguous" if ambiguous else "month_day"
            return PartialDate(m.group(0), rule, _fixed(month, day), _year(m["y"], now))

    for m in NAMED_MONTH_DAY.finditer(name):
        if not is_named_date(m) or (year_required and not m["y"]):
            continue
        month = _MONTH_NUMBERS[(m["mon"] or m["mon2"]).lower()]
        day = int(m["d"] or m["d2"])
        return PartialDate(m.group(0), "month_name_day", _fixed(month, day), _year(m["y"], now))

    if year_required:
        return None
    for rule, pattern, resolve in _HOLIDAYS:
        hit = pattern.search(name)
        if hit and not _names_another_month(name, resolve(2001).month):
            return PartialDate(hit.group(0), rule, resolve)

    return None


_MONTH_WORD = re.compile(rf"\b(?:{_MONTH_NAMES})\b", re.IGNORECASE)


def _names_another_month(name: str, month: int) -> bool:
    """ "Christmas in July" and "Guitar Christmas - January" are not December 25th."""
    return any(
        _MONTH_NUMBERS[m.group(0).lower()] != month and not month_is_a_name(name, m.start())
        for m in _MONTH_WORD.finditer(name)
    )


def date_window(dt: datetime, rule: str) -> tuple[datetime, datetime]:
    """The span a folder date really stands for: its year, month, or day."""
    if rule == "bare_year" or (rule == "season" and dt.month == 1):
        # Winter is the whole year: "Winter 2008" may be January or December.
        return datetime(dt.year, 1, 1), datetime(dt.year + 1, 1, 1)
    if rule == "season":
        start = datetime(dt.year, dt.month - 1, 1)
        return start, datetime(dt.year, dt.month + 2, 1)
    if rule == "year_month":
        start = datetime(dt.year, dt.month, 1)
        return start, (start + timedelta(days=32)).replace(day=1)
    start = datetime(dt.year, dt.month, dt.day)
    return start, start + timedelta(days=1)
