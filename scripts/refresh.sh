#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -c 'from pathlib import Path; import sys; sys.path.insert(0, str(Path.cwd())); from export_stories import export_json; print(export_json(Path("story-data/stories.sqlite3"), Path("data/stories.json")))'
