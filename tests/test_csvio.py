"""The two encodings, and the round trip through a spreadsheet."""

from __future__ import annotations

from pathlib import Path

from photofix.csvio import (
    read_review_csv,
    write_exiftool_csv,
    write_review_csv,
)


def test_review_csv_has_a_bom_so_excel_decodes_utf8(tmp_path: Path) -> None:
    path = tmp_path / "files.csv"
    write_review_csv(path, ["a"], [{"a": "Café"}])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf"), "without the BOM Excel shows 'CafÃ©'"


def test_applied_csv_has_no_bom(tmp_path: Path) -> None:
    """A BOM turns SourceFile into \\ufeffSourceFile and exiftool matches no column.

    It then reports success having written nothing, which is the most dangerous
    possible failure: a green run that did not happen.
    """
    path = tmp_path / "applied.csv"
    write_exiftool_csv(path, ["SourceFile", "EXIF:DateTimeOriginal"], [{"SourceFile": "a.jpg"}])
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.startswith(b"SourceFile")


def test_review_csv_quotes_every_field(tmp_path: Path) -> None:
    """Excel's delimiter follows the locale; keyword values contain semicolons."""
    path = tmp_path / "f.csv"
    write_review_csv(path, ["a", "b"], [{"a": "x;y", "b": "1"}])
    text = path.read_text(encoding="utf-8-sig")
    assert '"x;y"' in text
    assert '"1"' in text


def test_round_trip_preserves_semicolons_accents_and_commas(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    row = {"SourceFile": "C:/Café/a.jpg", "kw": "Zürich;Portland, OR;Noël"}
    write_review_csv(path, ["SourceFile", "kw"], [row])
    back = list(read_review_csv(path))
    assert back == [row]


def test_read_tolerates_a_file_resaved_without_a_bom(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_text('"a","b"\n"1","2"\n', encoding="utf-8")
    assert list(read_review_csv(path)) == [{"a": "1", "b": "2"}]


def test_read_strips_padded_headers(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    path.write_text("a , b\n1,2\n", encoding="utf-8")
    assert list(read_review_csv(path)) == [{"a": "1", "b": "2"}]


def test_booleans_render_as_one_and_zero(tmp_path: Path) -> None:
    path = tmp_path / "f.csv"
    write_review_csv(path, ["apply"], [{"apply": True}, {"apply": False}])
    assert [r["apply"] for r in read_review_csv(path)] == ["1", "0"]


def test_none_renders_as_empty_not_the_string_none(tmp_path: Path) -> None:
    """ "None" in a date cell would be parsed as a value, not as absence."""
    path = tmp_path / "f.csv"
    write_review_csv(path, ["d"], [{"d": None}])
    assert next(iter(read_review_csv(path)))["d"] == ""


def test_exiftool_csv_writes_columns_in_the_given_order(tmp_path: Path) -> None:
    path = tmp_path / "applied.csv"
    cols = ["SourceFile", "EXIF:DateTimeOriginal", "IPTC:Keywords"]
    write_exiftool_csv(path, cols, [{"SourceFile": "a.jpg", "IPTC:Keywords": "x;y"}])
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(cols)


def test_exiftool_csv_leaves_absent_values_empty(tmp_path: Path) -> None:
    """Empty means "don't touch"; anything else would be an instruction."""
    path = tmp_path / "applied.csv"
    write_exiftool_csv(path, ["SourceFile", "EXIF:DateTimeOriginal"], [{"SourceFile": "a.jpg"}])
    assert path.read_text(encoding="utf-8").splitlines()[1] == "a.jpg,"
