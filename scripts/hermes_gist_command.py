#!/usr/bin/env python3
"""Use the configured Hermes model as the local STORY_GIST_COMMAND adapter."""
from __future__ import annotations

import os
import json
import re
import subprocess
import sys

prompt = sys.stdin.read()
if not prompt.strip():
    raise SystemExit("empty prompt")
model = os.environ.get("STORY_GIST_MODEL", "gpt-5.6-luna")
provider = os.environ.get("STORY_GIST_PROVIDER", "openai-codex")
command = [
    "hermes", "chat", "-Q", "--source", "tool", "--max-turns", "1",
    "--toolsets", "safe", "--provider", provider, "--model", model, "-q", prompt,
]
completed = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False, env={
    key: value for key, value in os.environ.items() if not key.startswith("HERMES_SESSION_")
})
if completed.returncode:
    sys.stderr.write(completed.stderr or completed.stdout)
    raise SystemExit(completed.returncode)
lines = [line for line in completed.stdout.splitlines() if not line.startswith("session_id:")]
result = "\n".join(lines).strip()
match = re.search(r"(\{.*\})\s*$", result, re.DOTALL)
if match:
    try:
        result = json.dumps(json.loads(match.group(1)), ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        pass
if not result:
    raise SystemExit("Hermes returned no gist")
print(result)
