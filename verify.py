#!/usr/bin/env python3
"""Phase 3 entry point. See `photofix.verify` for the implementation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from photofix.verify import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
