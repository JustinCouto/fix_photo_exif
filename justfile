# `just` defaults to `sh -cu` everywhere, which on Windows would need sh.exe from
# Git for Windows. Pointing at pwsh removes that dependency. macOS ignores this.
set windows-shell := ["pwsh", "-NoProfile", "-Command"]

default:
    @just --list

# Resolve exiftool and print its version. Runs before the test suite because an
# unresolvable binary makes the integration tests *skip* rather than fail, which
# would otherwise report green while verifying nothing.
preflight:
    uv run python -c "import shutil,subprocess,sys; p=shutil.which('exiftool'); print('exiftool:', p or 'NOT FOUND'); sys.exit(0) if p else sys.exit('exiftool not on PATH — on Windows, restart the shell after installing')"
    uv run exiftool -ver

# Confirm the exiftool behaviors the design depends on. Run once per machine.
probe:
    uv run python tools/phase0_probe.py

test:
    uv run pytest -q

# Full gate: lint, types, and tests, with the exiftool preflight up front.
check: preflight
    uv run ruff check .
    uv run ruff format --check .
    uv run mypy
    uv run pytest -q

fmt:
    uv run ruff format .
    uv run ruff check --fix .

scan ROOT OUT="out":
    uv run python scan.py --root "{{ROOT}}" --out-dir "{{OUT}}"

apply OUT="out" *FLAGS:
    uv run python apply.py --files-csv "{{OUT}}/files.csv" --folders-csv "{{OUT}}/folders.csv" {{FLAGS}}

verify ROOT OUT="out":
    uv run python verify.py --root "{{ROOT}}" --applied-csv "{{OUT}}/applied.csv"
