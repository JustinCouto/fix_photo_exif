"""Extended attributes survive exiftool replacing the file."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from photofix import xattrs

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="no xattr support")

NAME = "user.photofix.test" if sys.platform == "linux" else "com.photofix.test"


def test_attributes_round_trip(tmp_path: Path) -> None:
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    if xattrs.restore(f, {NAME: b"\x00binary\xff"}):
        pytest.skip("this filesystem refuses extended attributes")
    saved = xattrs.read_all(f)
    assert saved[NAME] == b"\x00binary\xff"

    f.unlink()
    f.write_bytes(b"y")
    assert NAME not in xattrs.read_all(f)
    assert xattrs.restore(f, saved) == 0
    assert xattrs.read_all(f)[NAME] == b"\x00binary\xff"


def test_a_missing_file_reads_as_no_attributes(tmp_path: Path) -> None:
    assert xattrs.read_all(tmp_path / "gone.jpg") == {}
