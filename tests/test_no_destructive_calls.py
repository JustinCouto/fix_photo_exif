"""Enforce the non-goals structurally rather than by convention.

The tool must never move, rename, or delete anything in the archive. That is a
promise about code that does not exist, which no behavioral test can check —
a rename introduced in a rarely-hit branch would pass every other test in this
suite. So the AST is walked instead, and the check fails on the call being
present at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "photofix"

#: Attribute calls that move, rename, or destroy files.
FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        "unlink",
        "rmdir",
        "rmtree",
        "remove",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "move",
        "copytree",
        "chmod",
        "chown",
        "truncate",
    }
)

#: Modules that are allowed to call them, and why.
EXEMPT_MODULES: frozenset[str] = frozenset(
    {
        # Deletes scan.db under --fresh. Its own artifact, never the archive.
        "scan.py",
        # Renames its own applied.csv.partial over applied.csv, never the archive.
        "csvio.py",
    }
)

#: exiftool flags that would defeat the safety model.
FORBIDDEN_FLAGS: tuple[tuple[str, str], ...] = (
    ("-overwrite_original", "destroys the _original backups, which are the only undo path"),
    ("-overwrite_original_in_place", "same, without even the temp-file safety"),
    ("-delete_original", "deletes the backups after the fact"),
    ("-all=", "wipes every tag"),
    ("-filename<", "renames files, which is a non-goal"),
    ("-directory<", "moves files, which is a non-goal"),
)


def python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def test_there_is_something_to_check() -> None:
    assert python_files(), "the AST scan found no source files, so it proves nothing"


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_destructive_filesystem_calls(path: Path) -> None:
    if path.name in EXEMPT_MODULES:
        pytest.skip(f"{path.name} is exempt: it manages its own artifacts, not the archive")

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = [
        f"line {node.lineno}: {ast.unparse(node.func)}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in FORBIDDEN_ATTRS
        # str.replace and dict/list method names collide with os.replace; only
        # flag calls on something that plausibly names a path or a fs module.
        and _looks_like_filesystem(node.func)
    ]
    assert not offenders, f"{path.name} contains destructive filesystem calls: {offenders}"


def _looks_like_filesystem(func: ast.Attribute) -> bool:
    """Distinguish `os.remove(p)` from `text.replace('a', 'b')`."""
    if func.attr == "replace" or func.attr == "truncate":
        value = func.value
        if isinstance(value, ast.Name) and value.id in {"os", "shutil", "path", "p", "file"}:
            return True
        return isinstance(value, ast.Call) and "Path" in ast.unparse(value)
    return True


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_no_forbidden_exiftool_flags(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for flag, why in FORBIDDEN_FLAGS:
        # Allow the flag to be named in a comment, a docstring, or a denylist —
        # this file and exiftool.py both discuss them deliberately.
        for line in text.splitlines():
            stripped = line.strip()
            if flag not in stripped:
                continue
            if stripped.startswith(("#", '"', "'", "*")) or "assert" in stripped:
                continue
            raise AssertionError(f"{path.name}: {flag} appears in live code — {why}\n  {stripped}")


def test_dash_f_only_travels_with_the_delete_marker(tmp_path: Path) -> None:
    """With -csv=, -f makes a cell of "-" a deletion, and with an empty
    MissingTagValue every blank cell. Empty means "leave this alone" across the
    whole design, so -f is safe only with MissingTagValue set to the marker."""
    import subprocess

    from photofix.exiftool import DELETE_MARKER, ExifTool

    seen: list[list[str]] = []

    class Capturing(ExifTool):
        def __init__(self) -> None:
            self.work_dir = tmp_path
            self.timeout = 1.0

        def _run(self, args: object, *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
            seen.append(list(args))  # type: ignore[call-overload]
            return subprocess.CompletedProcess([], 0, "", "")

    Capturing().apply_csv(tmp_path / "applied.csv", ["a.jpg"])
    args = seen[0]
    assert DELETE_MARKER and DELETE_MARKER.strip() not in ("", "-")
    assert args.count("-f") == 1
    assert args[args.index("-f") + 1 : args.index("-f") + 3] == ["-api", f"MissingTagValue={DELETE_MARKER}"]


def test_the_only_module_invoking_exiftool_is_exiftool_py() -> None:
    """One chokepoint, so the flag audits above cover every invocation."""
    offenders = []
    for path in python_files():
        if path.name == "exiftool.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "subprocess." in text:
            offenders.append(path.name)
    assert not offenders, f"these modules call subprocess directly: {offenders}"
