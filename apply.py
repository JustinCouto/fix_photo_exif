#!/usr/bin/env python3
"""Phase 2 entry point. See `photofix.apply` for the implementation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from photofix.apply import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
