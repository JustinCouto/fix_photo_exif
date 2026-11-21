"""Turn folder names and filenames into proposed titles, keywords, and dates.

Folder names are the only description most archive photos have, so this is where
"IMG_4471.jpg" becomes something searchable. A filename that is a real
description ("Barney opening presents.jpg") beats its folder's name. It is all
pure guesswork, which is why every output is reviewable and nothing here is
ever applied unattended.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Final

from .datenames import (
    COARSE_FOLDER_RULES,
    HOLIDAY_PATTERNS,
    MONTH_NAMES,
    NAMED_MONTH_DAY,
    NOT_A_DATE_AFTER,
    SHORT_YEAR_AFTER_WORD,
    date_window,
    is_camera_sequence,
    is_named_date,
    month_is_a_name,
    parse_filename_date,
    parse_folder_date,
    parse_folder_partial,
    strip_extension,
    year_span,
)
from .models import DateCandidate, FolderProposal

#: Folders whose names carry no information. Matched against the name after
#: casefolding and separator collapsing.
JUNK_NAMES: Final[frozenset[str]] = frozenset(
    {
        "new folder",
        "untitled",
        "untitled folder",
        "untitled export",
        "folder",
        "photos",
        "photo",
        "pictures",
        "pics",
        "images",
        "img",
        "media",
        "misc",
        "miscellaneous",
        "stuff",
        "things",
        "various",
        "random",
        "unsorted",
        "sorted",
        "new",
        "old",
        "temp",
        "tmp",
        "test",
        "downloads",
        "download",
        "desktop",
        "documents",
        "my pictures",
        "my photos",
        "camera roll",
        "camera uploads",
        "camera",
        "dcim",
        "imported",
        "import",
        "imports",
        "export",
        "exports",
        "exported",
        "backup",
        "backups",
        "copy",
        "copies",
        "originals",
        "original",
        "edited",
        "edits",
        "final",
        "finals",
        "archive",
        "archives",
        "saved",
        "to sort",
        "to be sorted",
        "iphone",
        "ipad",
        "android",
        "whatsapp images",
        "whatsapp",
        "screenshots",
        "screenshot",
        "downloads folder",
        "recovered files",
        "found",
        "unknown",
        "scans",
        "scan",
        # Export and sync containers. "Photos from 2015" is Google Takeout's.
        "photos from",
        "pictures from",
        "pics from",
        "images from",
        "takeout",
        "google takeout",
        "google photos",
        "icloud",
        "icloud photos",
        "icloud photo library",
        "apple photos",
        "photo library",
        "photos library",
        "snapchat",
        "snapchat memories",
        "memories",
        "facebook",
        "facebook photos",
        "instagram",
        "instagram photos",
        "messenger",
        "sent",
        "received",
        # Curation and tool containers: they say how a photo was handled, not what it shows.
        "favorites",
        "favourites",
        "faves",
        "favs",
        "selects",
        "selected",
        "picks",
        "best",
        "lightroom",
        "lightroom export",
        "lightroom exports",
        "videos",
        "video",
        "movies",
        "old computer",
        "old laptop",
        "backup drive",
        "external drive",
        "hard drive",
    }
)

#: Device dump folders: DCIM subdirectories and card-reader spill.
DEVICE_DUMP: Final = re.compile(
    r"^(?:\d{3}(?:[A-Z]{4,5}|CANON|NIKON|MSDCF|OLYMP|APPLE|ANDRO|_FUJI|PENTX|GOPRO)"
    r"|\d{3}ND\d{3}|DCIM|MISC|PRIVATE|AVCHD|CANONMSC)$",
    re.IGNORECASE,
)
#: Camera folder names with a model code: "100_PANA", "100NCD90", "100EOS5D".
#: Case-sensitive, because cameras write them in capitals and "365Days" is a name.
_CAMERA_FOLDER: Final = re.compile(r"^\d{3}_?[A-Z0-9]{4,5}$")

#: Containers named for when they were made, not when their photos were taken:
#: "takeout-20200101T120000Z-001", "Backup Jan 2012", "Export 2019-03-02".
_MADE_ON = re.compile(r"^\W*(?:takeout|exports?|exported|backups?|downloads?|icloud)\b", re.IGNORECASE)

#: Tokens dropped from keywords: too common to be worth searching on.
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "in",
        "at",
        "on",
        "to",
        "for",
        "with",
        "from",
        "by",
        "my",
        "our",
        "his",
        "her",
        "its",
        "their",
        "is",
        "was",
        "were",
        "are",
        "be",
        "been",
        "this",
        "that",
        "these",
        "those",
        "it",
        "as",
        "into",
        "onto",
        "pics",
        "pictures",
        "photos",
        "images",
        "img",
        "jpg",
        "copy",
        "new",
        "folder",
        "misc",
        "various",
        "final",
        "edited",
        "originals",
    }
)

_SEPARATORS = re.compile(r"[\s_\-–—,&+/\\.]+")
#: Titles keep "&", "+", "," and "Mr." because "Mom & Dad" and "Portland, OR"
#: read wrong without them. A dot joining two words ("Christmas.Party") still
#: splits; one between digits ("2.5") is a decimal point, and a hyphen or slash
#: between digits ("6-8 Grade", "1/2 Marathon") is part of the number.
_TITLE_SEPARATORS = re.compile(r"[\s_–—\\]+|(?<!\d)[-/]+|[-/]+(?!\d)|(?<!\d)\.(?=[^\s.])|\.(?=[^\s.\d])")
#: The same for a name that already uses spaces, where a hyphen inside a word
#: is meant: "Mother-in-Law Visit", "D-Day", "I-95". Without spaces
#: ("hawaii-trip-2019") hyphens are the separators.
_SPACED_TITLE_SEPARATORS = re.compile(
    r"[\s_–—\\]+|(?<!\d)/+|/+(?!\d)|(?<=\s)-+|-+(?=\s)|^-+|-+$|(?<!\d)\.(?=[^\s.])|\.(?=[^\s.\d])"
)
#: "U.S.", "A.J.", "Mr." keep their dots.
_ABBREVIATION = re.compile(
    r"(?<![A-Za-z])(?:[A-Za-z]\.){2,}|\b(?:Mr|Mrs|Ms|Dr|St|Jr|Sr|Mt|Ft)\.", re.IGNORECASE
)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.!?])")
#: Quotes and brackets clinging to the edge of a token, e.g. "(Hawaii)".
_EDGE_PUNCT = re.compile(r"^['\"(\[]+|['\")\]]+$")
_MULTISPACE = re.compile(r"\s{2,}")
#: Trailing " (1)", " - Copy", " copy 2" that duplication tools leave behind.
_DUP_SUFFIX = re.compile(r"(?:\s*\(\d+\)|\s*-?\s*cop(?:y|ies)(?:\s*\d+)?)+$", re.IGNORECASE)
#: The prefix Windows and Google Drive give duplicated files.
_COPY_OF = re.compile(r"^\s*(?:copy\s+of\s+)+", re.IGNORECASE)

#: Numeric dates that the date parser does not use but that still do not belong
#: in a title: "12.21", "12-25-2021", "2021.12", "3-2015", a bare year, "'21".
#: Month names are left alone. Plain numbers are too, so "Route 66" survives,
#: and so are single-digit pairs, which are more often "1/2" or "3.5" than dates.
_DATE_FRAGMENT = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"\d{4}[-_./]\d{1,2}(?:[-_./]\d{1,2})?"
    r"|\d{1,2}[-_./]\d{1,2}[-_./](?:\d{4}|\d{2})"
    r"|\d{1,2}[-_./](?:19|20)\d{2}"
    r"|\d{2}[-_./]\d{2}"
    r"|(?:19|20)\d{2}(?:\d{4})?"
    r"|['\u2019]\d{2}"
    r")(?![A-Za-z0-9])" + NOT_A_DATE_AFTER,
    re.IGNORECASE,
)
_CIRCA = re.compile(
    r"(?<![\w.])(?:circa|ca\.|c\.|approx\.?|approximately|around)\s*(?=(?:19|20)\d{2})", re.IGNORECASE
)
_EMPTY_BRACKETS = re.compile(r"[(\[][\s\-–—,./~?]*[)\]]")
_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)(?:'s)?", re.IGNORECASE)

#: Prefixes cameras, phones, and apps put on filenames. Dropped from a filename title.
CAMERA_PREFIXES: Final[frozenset[str]] = frozenset(
    {"img", "dsc", "dscn", "dscf", "imgp", "pict", "sam", "pxl", "mvimg", "mvi", "vid", "pano", "burst"}
    | {"gopr", "dji", "wa", "fb", "hdr", "crw", "dcp", "sta", "cimg", "kimg"}
)

_MONTHS: Final[frozenset[str]] = frozenset(
    {"january", "february", "march", "april", "may", "june", "july", "august", "september"}
    | {"october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug"}
    | {"sep", "sept", "oct", "nov", "dec"}
)
_WEEKDAYS: Final[frozenset[str]] = frozenset(
    {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    | {"mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun"}
)

#: Words that can appear in a filename without describing the photo. A filename
#: made only of these, numbers, and dates is not a real name.
GENERIC_FILE_WORDS: Final[frozenset[str]] = frozenset(
    {word for name in JUNK_NAMES for word in name.split()}
    | {"image", "picture", "pic", "screen", "shot", "scan", "scanned", "edit", "file", "snapshot"}
    | {"resized", "small", "large", "thumb", "thumbnail", "portrait", "live", "untitled", "unnamed"}
    | {"fullsizerender", "received", "snapchat", "signal", "messenger", "facebook", "instagram"}
    | {"canon", "nikon", "kodak", "sony", "olympus", "fuji", "fujifilm", "panasonic", "lumix", "pentax"}
    | {"samsung", "pixel", "gopro", "capture", "snip", "snipping", "foto", "fotos", "cover", "motion"}
    | {"collage", "animation", "effects", "stylized"}
    | {"attachment", "attachments", "maxresdefault", "hqdefault", "doc", "camscanner", "enhanced", "nr"}
    | {"creation", "ios", "computer", "laptop", "drive", "phone", "eos", "export", "exports", "saves"}
    | {"am", "pm"}
    | CAMERA_PREFIXES
    | STOPWORDS
    | _MONTHS
    | _WEEKDAYS
)

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


#: Boilerplate cameras, scanners, and apps write into description and title
#: fields on every shot. None of it counts as a real title.
_PLACEHOLDER_TITLES: Final[frozenset[str]] = frozenset(
    {"sony dsc", "exif_jpeg_picture", "default", "image", "picture", "photo", "untitled", "dcim"}
    | {"samsung", "leica", "lg digital camera", "camera", "scan", "scanned image"}
)
#: The whole title, so "Dad with his new digital camera" is a real title.
_DIGITAL_CAMERA = re.compile(
    r"^(?:[\w-]+\s+){0,2}digital\s+(?:still\s+)?camera$|^(?:dscn?|dscf|img|pict)[\s_-]*\d*$", re.IGNORECASE
)


def is_placeholder_title(text: str) -> bool:
    """Whether an existing title is blank or boilerplate, and so safe to replace."""
    stripped = text.strip()
    return not stripped or stripped.casefold() in _PLACEHOLDER_TITLES or bool(_DIGITAL_CAMERA.match(stripped))


def _named_date(m: re.Match[str]) -> str:
    if not is_named_date(m):
        return m.group(0)  # "Aunt May 80th Birthday"
    month = (m["mon"] or m["mon2"]).casefold()
    if month.startswith("jul") and int(m["d"] or m["d2"]) == 4:
        return " 4th of July "  # a holiday's name, not a date
    return " "


#: A month name beside a year ("Jan 2010", "Jan-Mar 2020", "2019 March"). The
#: year goes with the other fragments; a month alone is not a title.
_MONTH_BY_YEAR = re.compile(
    rf"\b(?:{MONTH_NAMES})\.?(?:\s*(?:-|\u2013|to|thru|through|&|and)\s*(?:{MONTH_NAMES})\.?)?,?"
    rf"(?=[\s_-]+(?:(?:19|20)\d{{2}}|['\u2019]\d{{2}})(?!\d))"
    rf"|(?<=(?<!\d)(?:19|20)\d\d)[\s_-]+(?:{MONTH_NAMES})\b",
    re.IGNORECASE,
)
#: Stands in for a removed date, so the words that joined two of them can go too.
_GAP = "\x00"
_JOINED_GAPS = re.compile(
    rf"{_GAP}[\s,]*(?:-|\u2013|\u2014|to|thru|through|until|till|and|&)[\s,]*{_GAP}", re.I
)


_CHRISTMAS_IN = re.compile(r"\b(?:christmas|xmas)\s+in\s+$", re.IGNORECASE)


def _month_by_year(m: re.Match[str]) -> str:
    if month_is_a_name(m.string, m.start()):  # "Aunt May 2015"
        return m.group(0)
    if _CHRISTMAS_IN.search(m.string[: m.start()]):  # "Christmas in July 2019"
        return m.group(0)
    return _GAP


def _short_year(m: re.Match[str]) -> str:
    return m.group(0)[: m.start("yy") - m.start()] + _GAP  # "Xmas 99" -> "Xmas"


def strip_date_fragments(text: str) -> str:
    text = NAMED_MONTH_DAY.sub(_named_date, _CIRCA.sub(" ", text))
    text = SHORT_YEAR_AFTER_WORD.sub(_short_year, _MONTH_BY_YEAR.sub(_month_by_year, text))
    text = _DATE_FRAGMENT.sub(_GAP, text)
    while _JOINED_GAPS.search(text):
        text = _JOINED_GAPS.sub(_GAP, text)
    return _EMPTY_BRACKETS.sub(" ", text.replace(_GAP, " "))


def _tidy_title(text: str) -> str:
    """Separators to single spaces, keeping punctuation that belongs to the words."""
    separators = _SPACED_TITLE_SEPARATORS if " " in text.strip() else _TITLE_SEPARATORS
    pieces, last = [], 0
    for m in _ABBREVIATION.finditer(text):
        pieces += [separators.sub(" ", text[last : m.start()]), m.group(0)]
        last = m.end()
    text = "".join([*pieces, separators.sub(" ", text[last:])])
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", text)
    text = _MULTISPACE.sub(" ", _EMPTY_BRACKETS.sub(" ", text))
    # Removing a date can orphan the punctuation beside it: "2021, Christmas".
    text = text.lstrip(" ,.&+;:").rstrip(" ,&+;:")
    if text.endswith(".") and not _ABBREVIATION.fullmatch(text.rsplit(" ", 1)[-1]):
        text = text.rstrip(". ")  # "Jake.Birthday.2019"
    return text


def _collapse(name: str) -> str:
    return _SEPARATORS.sub(" ", name).strip().casefold()


def _is_junk_phrase(collapsed: str) -> bool:
    # "Untitled 2", "Scans 3", "Takeout Z 001", "IMG 0001 0500": a junk name with
    # sequence numbers or stray letters after it is still junk.
    return collapsed in JUNK_NAMES or re.sub(r"(?:\s+(?:\d+|[a-z]))+$", "", collapsed) in JUNK_NAMES


def _all_generic(title: str) -> bool:
    """Whether every word is one a camera, app, or container would use: "Kodak Scans"."""
    words = re.findall(r"[^\W\d_]+", title.casefold())
    return bool(words) and all(w in GENERIC_FILE_WORDS for w in words)


def is_junk_name(name: str) -> bool:
    """Whether a folder name is worth deriving anything from."""
    stripped = _DUP_SUFFIX.sub("", name).strip()
    collapsed = _collapse(stripped)
    if not collapsed:
        return True
    if _is_junk_phrase(collapsed):
        return True
    if DEVICE_DUMP.match(stripped) or _CAMERA_FOLDER.match(stripped):
        return True
    # A name made only of digits and separators is a date or a sequence number,
    # never a description.
    if re.fullmatch(r"[\d\s_\-–—.]+", stripped):
        return True
    # Single letters, and "a1"-style shorthand.
    return len(collapsed.replace(" ", "")) <= 2


#: Lowercase inside a title unless first or last: "Barney on Christmas Eve".
_MINOR_WORDS: Final[frozenset[str]] = frozenset(
    {"a", "an", "the", "and", "but", "or", "nor", "for"}
    | {"at", "by", "in", "of", "on", "to", "as", "vs", "via", "from", "with", "into", "onto", "per"}
    # Particles in place and family names: "Noël à Zürich", "Cinco de Mayo".
    | {"à", "de", "du", "des", "la", "le", "van", "von", "der", "den", "di", "da", "y"}
)

#: Kept in capitals when a SHOUTED name is lowered: "4TH OF JULY BBQ".
_ACRONYMS: Final[frozenset[str]] = frozenset(
    {"BBQ", "NYC", "LA", "USA", "UK", "DC", "NY", "CD", "DVD", "TV", "VHS", "RV", "ATV", "DJ", "BFF"}
    | {"YMCA", "NASA", "MLB", "NFL", "NBA", "NHL", "UCLA", "USC", "LSU", "BYU", "PTA", "4H", "HS"}
)


def _capitalize_part(word: str) -> str:
    if word[:1].isdigit():
        return word  # "80th", not "80Th"
    for i, c in enumerate(word):
        if c.isalpha():
            return word[:i] + c.upper() + word[i + 1 :]
    return word


def _capitalize(word: str) -> str:
    """Each part of a hyphenated word, bar minor ones: "Mother-in-Law", "D-Day"."""
    parts = word.split("-")
    return "-".join(p if i and p in _MINOR_WORDS else _capitalize_part(p) for i, p in enumerate(parts))


#: Shouted words that are words, not acronyms, even when short: "XMAS 2004".
_SHOUTED_WORDS: Final[dict[str, str]] = {"XMAS": "Xmas"}


def _retitle(text: str) -> str:
    """Title case, without destroying deliberate capitalization.

    Only all-lowercase words are re-cased, so "iPhone" and "BBQ" survive. A
    SHOUTED name is lowered first, because there every capital is noise, apart
    from known acronyms and a lone short word ("NYC 2019", "CD 1").
    """
    words = [_SHOUTED_WORDS.get(w, w) for w in text.split()]
    letters = [c for c in " ".join(words) if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        alpha = [w for w in words if any(c.isalpha() for c in w)]
        if not (len(alpha) == 1 and sum(c.isalpha() for c in alpha[0]) <= 4):
            words = [w if _EDGE_PUNCT.sub("", w.strip(",.&+;:")) in _ACRONYMS else w.lower() for w in words]
    out = []
    for i, word in enumerate(words):
        core = _EDGE_PUNCT.sub("", word.strip(",.&+;:"))
        minor = 0 < i < len(words) - 1 and word.strip(",.") in _MINOR_WORDS
        if minor or any(c.isupper() for c in word):
            out.append(word)
        elif core.upper() in _ACRONYMS and core.casefold() not in _MINOR_WORDS:
            out.append(word.replace(core, core.upper()))  # "nyc" -> "NYC", but "la" stays a word
        else:
            out.append(_capitalize(word))
    return " ".join(out)


def derive_title(name: str, *, date_match: str = "") -> str:
    """Folder name minus its date and duplication suffixes."""
    # Fragments first: a date_match of "2019" taken out of "2019-07 Hawaii"
    # would strand the "07", which no longer looks like a date on its own.
    text = _CODE.sub(" ", strip_date_fragments(_COPY_OF.sub("", name)))
    if date_match:
        text = text.replace(date_match, " ")
    text = _tidy_title(_DUP_SUFFIX.sub("", text.strip()))
    # Leftovers like "7", "?" or "~" are not a title: it needs a real word.
    if not _WORD.search(text):
        return ""
    return _retitle(text)


_WORD = re.compile(r"[^\W\d_]{2}")
#: Android screenshots end in the app's package: "..._com.facebook.katana".
_APP_PACKAGE = re.compile(r"(?<![^\W_])(?:com|org|net|io)\.[a-z0-9_]+(?:\.[a-z0-9_]+)+", re.IGNORECASE)
#: Words whose number is part of the name, not a sequence: "Age 2", "Game 3".
_NUMBERED_WORDS: Final[frozenset[str]] = frozenset(
    {"age", "aged", "day", "week", "part", "game", "grade", "round", "chapter", "vol", "volume"}
    | {"route", "highway", "hwy", "number", "no", "level", "stage", "lap"}
)
#: A token with a long digit run is a code, not a word: "20200101T120000Z".
_CODE = re.compile(r"(?<![\w'])[^\W_]*\d{5,}[^\W_]*(?![\w'])")


def derive_file_title(filename: str) -> str:
    """A title from the filename, or "" when the name is not a real description.

    "IMG_4471.jpg", "20205-01.jpg", "Screenshot 2021-01-01 at 10.00.00 AM.png",
    and "Photo Dec 25.jpg" all return "" so the folder's title is used instead.
    "Barney opening presents.jpg" returns "Barney opening presents".
    """
    stem = strip_extension(filename)
    if is_camera_sequence(stem) or _UUID.match(stem):
        return ""
    text = _APP_PACKAGE.sub(" ", stem)
    candidate = parse_filename_date(filename)
    if candidate is not None:
        text = text.replace(candidate.match, " ")
    text = _DUP_SUFFIX.sub("", strip_date_fragments(text).strip())
    text = _COPY_OF.sub("", text)

    kept: list[tuple[str, bool]] = []  # (token, is a bare number)
    cores: list[str] = []
    for token in _tidy_title(text).split():
        core = _EDGE_PUNCT.sub("", token.strip(",.&+;:"))
        if core.casefold() in CAMERA_PREFIXES:
            continue
        if re.fullmatch(r"\d+(?:\.\d+)?", core):  # "3", "2.5"
            kept.append((token, True))
        elif _ORDINAL.fullmatch(core) or not any(c.isdigit() for c in core):
            kept.append((token, False))
            if core and not core[0].isdigit():
                cores.append(core)
        # Anything else mixing letters and digits is a code like "WA0001".
    # A number at either end is a sequence ("Beach 3"); inside, it is part of
    # the name ("Top 10 Moments"), and so is one a word asks for ("Emma age 2").
    while kept and kept[0][1]:
        kept.pop(0)
    while kept and kept[-1][1] and not (len(kept) > 1 and kept[-2][0].casefold() in _NUMBERED_WORDS):
        kept.pop()
    if not any(len(w) >= 3 and w.casefold() not in GENERIC_FILE_WORDS for w in cores):
        return ""
    return _retitle(_tidy_title(" ".join(token for token, _ in kept)))


def derive_keywords(title: str) -> str:
    """Semicolon-joined keywords from a title.

    Semicolons, not commas: keyword values routinely contain commas
    ("Portland, OR") and a comma-separated field would split them into two
    wrong keywords. `-sep ";"` matches on the exiftool side.
    """
    seen: dict[str, str] = {}
    # Holidays and "St. Louis" are one keyword each, not "Christmas" and "Eve".
    for pattern in (*HOLIDAY_PATTERNS, _PLACE_PREFIX):
        for m in pattern.finditer(title):
            phrase = _retitle(m.group(0).lower()) if m.group(0).islower() else m.group(0)
            seen.setdefault(phrase.casefold(), phrase)
        title = pattern.sub(" ", title)
    for token in _KEYWORD_SEPARATORS.split(title):
        token = _EDGE_PUNCT.sub("", token)
        if not token or any(c.isdigit() for c in token) or len(token) < 3:
            continue
        key = token.casefold()
        if key in STOPWORDS or key in _KEYWORD_NOISE or key in seen:
            continue
        seen[key] = token
    return ";".join(seen.values())


#: Like _SEPARATORS, but a hyphen inside a word keeps it whole: "D-Day".
_KEYWORD_SEPARATORS = re.compile(r"[\s_–—,&+/\\.]+|(?<![^\W_])-|-(?![^\W_])")
_PLACE_PREFIX = re.compile(r"\b(?:St|Mt|Ft|Saint|Mount|Fort)\.?\s+[^\W\d_][\w'’]*", re.IGNORECASE)
#: Words that are not worth searching on by themselves.
_KEYWORD_NOISE: Final[frozenset[str]] = (
    _MONTHS
    | _WEEKDAYS
    | {"day", "days", "eve", "night", "year", "years", "year's", "old", "turns", "age", "part", "week"}
    | {"time", "misc", "more", "other", "others", "some", "random"}
)


def classify_folder(name: str, *, now: datetime | None = None) -> FolderProposal:
    """Decide what, if anything, a folder name contributes.

    `skip` suppresses the title and keywords but never the date. A folder named
    "2019-07-04" is junk as a description and excellent as a date source, and
    conflating the two would throw away the better signal of the two.

    A month and day ("12.21", "Dec 21", "Christmas Day") is combined with a
    year in the same name, so "2021 Christmas Day" dates to 2021-12-25 rather
    than 2021-01-01. Without a year it is carried as `partial` for a parent
    folder to complete. A full year-first date is never overridden by one.
    """
    now = now or datetime.now()
    # "Backup Jan 2012" is when the backup was made, not when its photos were taken.
    candidate = None if _MADE_ON.match(name) else parse_folder_date(name, now=now)
    date = candidate.dt if candidate else None
    date_match = candidate.match if candidate else ""
    date_rule = candidate.rule if candidate else ""
    # A match with words in it ("Summer 2008", "Xmas 99") is left for
    # strip_date_fragments, which takes the year and keeps the word.
    worded = candidate is not None and candidate.rule in COARSE_FOLDER_RULES and _WORD.search(date_match)
    title_match = "" if worded else date_match
    partial = None
    # "Grandpa (1920-1995)": no date here, and no holiday waiting for a year.
    span = year_span(name)

    if (
        span is None
        and not _MADE_ON.match(name)
        and (candidate is None or candidate.rule in COARSE_FOLDER_RULES)
    ):
        found = parse_folder_partial(name, now=now)
        if found is not None:
            year = found.year or (candidate.dt.year if candidate else None)
            if year is None:
                partial = found
            elif (combined := found.on(year, now=now)) is not None and _within(combined, candidate):
                date, date_rule = combined, found.rule
                date_match = found.match
                if candidate is not None and not found.year:
                    date_match = f"{found.match} + {candidate.match}"

    def proposal(title: str, keywords: str, skip: bool) -> FolderProposal:
        return FolderProposal(title, keywords, date, date_match, date_rule, skip, partial, span)

    if is_junk_name(name):
        return proposal("", "", True)
    title = derive_title(name, date_match=title_match)
    # "Misc 2019" and "Photos from 2015" are junk once the year is gone, and
    # "Kodak Scans" or "Canon EOS" never said anything about the photos.
    if not title or _is_junk_phrase(_collapse(title)) or _all_generic(title):
        return proposal("", "", True)
    return proposal(title, derive_keywords(title), False)


def _within(day: datetime, candidate: DateCandidate | None) -> bool:
    """Whether a holiday or month-day agrees with the month or season the name states.

    "Guitar Christmas - January 2011" is January, not December 25th.
    """
    if candidate is None or candidate.rule == "bare_year":
        return True
    start, end = date_window(candidate.dt, candidate.rule)
    return start <= day < end


#: Inner folders that only make sense under their parent: "Disney 2018/Day 1".
_SUBFOLDER_TITLE = re.compile(
    r"^(?:day|part|week|vol(?:ume)?|disc|disk|roll|set|batch|page|card)\s*\d+$", re.IGNORECASE
)


def is_subfolder_title(title: str) -> bool:
    return _SUBFOLDER_TITLE.match(title) is not None
