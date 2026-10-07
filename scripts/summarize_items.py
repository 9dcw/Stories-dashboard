#!/usr/bin/env python3
"""CLI wrapper for the Stage 3 summarizer."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from summarize_items import main

if __name__ == "__main__":
    main()
