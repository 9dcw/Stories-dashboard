#!/bin/bash
# Registry downloads can fail mid-layer; retry before touching the running app.
set -euo pipefail
image=${1:?Usage: pull-image.sh IMAGE}
for attempt in 1 2 3; do
  if docker pull "$image"; then
    exit 0
  fi
  if [ "$attempt" -eq 3 ]; then
    echo "Image download failed after $attempt attempts: $image" >&2
    exit 1
  fi
  delay=$((attempt * 5))
  echo "Image download failed; retrying in $delay seconds ($attempt/3)." >&2
  sleep "$delay"
done
