#!/usr/bin/env python3
"""Run polling, Stage 3 gisting, export, and the existing Pages deployment path.

The process intentionally prints only compact JSON statistics. Article bodies and
model prompts are kept inside this process and the Hermes adapter subprocess.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from export_stories import export_json
from polling import poll_sources
from story_store import StoryStore
from summarize_items import command_summarizer, summarize_items


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=False)


def deploy(root: Path, commit_message: str) -> dict[str, object]:
    result: dict[str, object] = {"attempted": True, "committed": False, "pushed": False}
    status = _run(["git", "status", "--porcelain", "--", "data/stories.json"], cwd=root)
    if status.returncode:
        raise RuntimeError(status.stderr.strip() or "git status failed")
    if not status.stdout.strip():
        return result
    added = _run(["git", "add", "--", "data/stories.json"], cwd=root)
    if added.returncode:
        raise RuntimeError(added.stderr.strip() or "git add failed")
    commit = _run(["git", "commit", "-m", commit_message], cwd=root)
    if commit.returncode:
        raise RuntimeError(commit.stderr.strip() or "git commit failed")
    result["committed"] = True
    pushed = _run(["git", "push", "origin", "main"], cwd=root)
    if pushed.returncode:
        raise RuntimeError(pushed.stderr.strip() or "git push failed")
    result["pushed"] = True
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "story-data" / "stories.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "stories.json")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--limit", type=int, help="limit gisting for a bounded validation run; daily default is all pending items")
    parser.add_argument("--no-deploy", action="store_true")
    parser.add_argument("--commit-message", default="chore: refresh story discovery snapshot")
    parser.add_argument("--log-file", type=Path, default=ROOT / "story-data" / "daily-refresh.jsonl")
    args = parser.parse_args(argv)
    if args.max_attempts < 1 or args.max_attempts > 5:
        parser.error("--max-attempts must be between 1 and 5")

    command = os.environ.get(
        "STORY_GIST_COMMAND",
        f"python3 {ROOT / 'scripts' / 'hermes_gist_command.py'}",
    )
    store = StoryStore(args.db)
    poll = poll_sources(store, retry_attempts=args.max_attempts, retry_delay=args.retry_delay)
    summary = {"attempts": 0, "items_checked": 0, "summaries_created": 0, "summaries_failed": 0}
    summarizer = command_summarizer(command)
    pending = store.list_unsummarized_items(limit=args.limit, active_only=True)
    item_ids = [row["item_id"] for row in pending]
    for attempt in range(args.max_attempts):
        current = summarize_items(store, item_ids=item_ids, summarizer=summarizer)
        summary["attempts"] += 1
        for key in ("items_checked", "summaries_created", "summaries_failed"):
            summary[key] += current[key]
        if current["summaries_failed"] == 0:
            break
        if attempt + 1 < args.max_attempts:
            time.sleep(args.retry_delay)
    summary["remaining_unsummarized"] = len(store.list_unsummarized_items(active_only=True))
    export_json(args.db, args.output)
    deployment = {"attempted": False, "committed": False, "pushed": False}
    if not args.no_deploy:
        deployment = deploy(ROOT, args.commit_message)

    stats = {"poll": poll, "summaries": summary, "exported": True, "deployment": deployment}
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    with args.log_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(stats, separators=(",", ":")) + "\n")
    print(json.dumps(stats, separators=(",", ":")))


if __name__ == "__main__":
    main()