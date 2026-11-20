"""Turn a folder name into a proposed title, keywords, and date.

Folder names are the only description most archive photos have, so this is where
"IMG_4471.jpg" becomes something searchable. It is also pure guesswork, which is
why every output is reviewable and nothing here is ever applied unattended.
"""

from __future__ import annotations

import re
from typing import Final

from .datenames import parse_folder_date
from .models import FolderProposal

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
    }
)

#: Device dump folders: DCIM subdirectories and card-reader spill.
DEVICE_DUMP: Final = re.compile(
    r"^(?:\d{3}(?:[A-Z]{4,5}|CANON|NIKON|MSDCF|OLYMP|APPLE|ANDRO|_FUJI|PENTX|GOPRO)"
    r"|\d{3}ND\d{3}|DCIM|MISC|PRIVATE|AVCHD|CANONMSC)$",
    re.IGNORECASE,
)

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
#: Quotes and brackets clinging to the edge of a token, e.g. "(Hawaii)".
_EDGE_PUNCT = re.compile(r"^['\"(\[]+|['\")\]]+$")
_MULTISPACE = re.compile(r"\s{2,}")
#: Trailing " (1)", " - Copy", " copy 2" that duplication tools leave behind.
_DUP_SUFFIX = re.compile(r"(?:\s*\(\d+\)|\s*-?\s*cop(?:y|ies)(?:\s*\d+)?)+$", re.IGNORECASE)


def _collapse(name: str) -> str:
    return _SEPARATORS.sub(" ", name).strip().casefold()


def is_junk_name(name: str) -> bool:
    """Whether a folder name is worth deriving anything from."""
    stripped = _DUP_SUFFIX.sub("", name).strip()
    collapsed = _collapse(stripped)
    if not collapsed:
        return True
    if collapsed in JUNK_NAMES:
        return True
    if DEVICE_DUMP.match(stripped):
        return True
    # A name made only of digits and separators is a date or a sequence number,
    # never a description.
    if re.fullmatch(r"[\d\s_\-–—.]+", stripped):
        return True
    # Single letters, and "a1"-style shorthand.
    return len(collapsed.replace(" ", "")) <= 2


def _retitle(text: str) -> str:
    """Normalize casing without destroying deliberate capitalization.

    SHOUTED and lowercased names are title-cased; anything with existing mixed
    case is left alone, because "iPhone backup" and "McDonalds BBQ" are already
    more correct than anything a re-caser would produce.
    """
    if not text:
        return ""
    letters = [c for c in text if c.isalpha()]
    if letters and (all(c.isupper() for c in letters) or all(c.islower() for c in letters)):
        return " ".join(word.capitalize() for word in text.split())
    return text


def derive_title(name: str, *, date_match: str = "") -> str:
    """Folder name minus its date and duplication suffixes."""
    text = name
    if date_match:
        text = text.replace(date_match, " ")
    text = _DUP_SUFFIX.sub("", text)
    text = _SEPARATORS.sub(" ", text)
    text = _MULTISPACE.sub(" ", text).strip()
    # A stripped name of only digits is a leftover sequence, not a title.
    if not text or text.isdigit():
        return ""
    return _retitle(text)


def derive_keywords(title: str) -> str:
    """Semicolon-joined keywords from a title.

    Semicolons, not commas: keyword values routinely contain commas
    ("Portland, OR") and a comma-separated field would split them into two
    wrong keywords. `-sep ";"` matches on the exiftool side.
    """
    seen: dict[str, str] = {}
    for token in _SEPARATORS.split(title):
        token = _EDGE_PUNCT.sub("", token)
        if not token or token.isdigit() or len(token) < 3:
            continue
        key = token.casefold()
        if key in STOPWORDS or key in seen:
            continue
        seen[key] = token
    return ";".join(seen.values())


def classify_folder(name: str) -> FolderProposal:
    """Decide what, if anything, a folder name contributes.

    `skip` suppresses the title and keywords but never the date. A folder named
    "2019-07-04" is junk as a description and excellent as a date source, and
    conflating the two would throw away the better signal of the two.
    """
    candidate = parse_folder_date(name)
    date_match = candidate.match if candidate else ""
    date_rule = candidate.rule if candidate else ""

    if is_junk_name(name):
        return FolderProposal(
            title="",
            keywords="",
            date=candidate.dt if candidate else None,
            date_match=date_match,
            date_rule=date_rule,
            skip=True,
        )

    title = derive_title(name, date_match=date_match)
    if not title:
        return FolderProposal(
            title="",
            keywords="",
            date=candidate.dt if candidate else None,
            date_match=date_match,
            date_rule=date_rule,
            skip=True,
        )

    return FolderProposal(
        title=title,
        keywords=derive_keywords(title),
        date=candidate.dt if candidate else None,
        date_match=date_match,
        date_rule=date_rule,
        skip=False,
    )
