import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.parametrize('failures,attempts,expected', [(0, 1, 0), (2, 3, 0), (3, 3, 1)])
def test_image_pull_retries_before_deployment(tmp_path, failures, attempts, expected):
    docker = tmp_path / 'docker'
    docker.write_text('''#!/bin/bash
set -eu
count=0
if [ -f "$PULL_TEST_STATE" ]; then count=$(cat "$PULL_TEST_STATE"); fi
count=$((count + 1))
echo "$count" > "$PULL_TEST_STATE"
[ "$1" = pull ]
[ "$2" = ghcr.io/example/app:tested ]
[ "$#" -eq 2 ]
if [ "$count" -le "$PULL_TEST_FAILURES" ]; then exit 1; fi
''')
    docker.chmod(0o700)
    sleeper = tmp_path / 'sleep'
    sleeper.write_text('#!/bin/bash\necho "$1" >> "$PULL_TEST_DELAYS"\n')
    sleeper.chmod(0o700)
    state, delays = tmp_path / 'state', tmp_path / 'delays'
    environment = os.environ | {
        'PATH': str(tmp_path) + os.pathsep + os.environ['PATH'],
        'PULL_TEST_STATE': str(state), 'PULL_TEST_DELAYS': str(delays),
        'PULL_TEST_FAILURES': str(failures),
    }
    script = Path(__file__).resolve().parents[1] / 'deploy/pull-image.sh'
    result = subprocess.run(['bash', str(script), 'ghcr.io/example/app:tested'],
                            env=environment, capture_output=True, text=True)
    assert result.returncode == expected
    assert int(state.read_text()) == attempts
    assert (delays.read_text().splitlines() if delays.exists() else []) == ['5', '10'][:attempts-1]
