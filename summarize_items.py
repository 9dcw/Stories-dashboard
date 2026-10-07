"""Fetch source pages and create bounded candidate gists."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from gist import MAX_INPUT_CHARS, PROMPT_VERSION, build_bounded_input, extract_html_text, fetch_url, prompt_for
from story_store import StoryStore


def command_summarizer(command: str) -> Callable[[str], str]:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("summarizer command is empty")

    def summarize(prompt: str) -> str:
        completed = subprocess.run(argv, input=prompt, text=True, capture_output=True, check=False, timeout=120)
        if completed.returncode:
            raise RuntimeError(completed.stderr.strip() or f"summarizer exited {completed.returncode}")
        result = completed.stdout.strip()
        if not result:
            raise ValueError("summarizer returned empty output")
        return result

    return summarize


def summarize_items(store: StoryStore, *, item_ids: list[int] | None = None, force: bool = False,
                    fetcher: Callable[[str], str] = fetch_url,
                    summarizer: Callable[[str], str] | None = None,
                    max_input_chars: int = MAX_INPUT_CHARS) -> dict[str, int]:
    rows = [store.get_item(item_id) for item_id in item_ids] if item_ids else (store.list_items() if force else store.list_unsummarized_items())
    if not force:
        rows = [row for row in rows if row.get("summary_status") != "COMPLETE"]
    result = {"items_checked": 0, "summaries_created": 0, "summaries_failed": 0}
    for item in rows:
        result["items_checked"] += 1
        try:
            html_content = fetcher(item["raw_url"])
            extracted, method = extract_html_text(html_content)
            bounded = build_bounded_input(item, extracted, max_input_chars)
            if len(extracted.strip()) < 40:
                raise ValueError("extracted text is too short")
            if summarizer is None:
                raise ValueError("no summarizer configured; set STORY_GIST_COMMAND or pass a summarizer")
            gist = summarizer(prompt_for(bounded.input_text)).strip()
            if not gist:
                raise ValueError("summarizer returned empty output")
            store.save_summary(item["item_id"], gist=gist, summary_status="COMPLETE", summary_prompt_version=PROMPT_VERSION,
                               extracted_char_count=bounded.extracted_char_count, input_char_count=bounded.input_char_count,
                               extraction_method=method)
            result["summaries_created"] += 1
        except Exception as exc:
            store.save_summary(item["item_id"], gist="", summary_status="FAILED", summary_prompt_version=PROMPT_VERSION,
                               extraction_method="fetch_failed" if isinstance(exc, (OSError, ValueError)) and "extract" not in str(exc).lower() else "",
                               summary_error=str(exc)[:500])
            result["summaries_failed"] += 1
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    root = Path(__file__).resolve().parent
    parser.add_argument("--db", type=Path, default=root / "story-data" / "stories.sqlite3")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--item", type=int)
    group.add_argument("--pending", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-input-chars", type=int, default=MAX_INPUT_CHARS)
    parser.add_argument("--command", default=os.environ.get("STORY_GIST_COMMAND", ""), help="local command reading prompt on stdin")
    args = parser.parse_args(argv)
    store = StoryStore(args.db)
    summarizer = command_summarizer(args.command) if args.command else None
    item_ids = [args.item] if args.item else None
    print(json.dumps(summarize_items(store, item_ids=item_ids, force=args.force, summarizer=summarizer,
                                     max_input_chars=args.max_input_chars), separators=(",", ":")))


if __name__ == "__main__":
    main()
