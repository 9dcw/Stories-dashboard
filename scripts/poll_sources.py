#!/usr/bin/env python3
"""CLI wrapper for Stage 2 polling."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polling import main


if __name__ == "__main__":
    main()
