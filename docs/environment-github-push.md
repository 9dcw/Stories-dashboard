# Stories Dashboard: GitHub pushes from this environment

Repository: https://github.com/9dcw/Stories-dashboard  
Production branch: `main`  
GitHub remote name: `github` (do not assume `origin` points to GitHub).

## Checkout locations

- Persistent checkout in this workspace: `/home/david/Documents/Codex/2026-10-25/the-promotion-process-doesn-t-have/stories-dashboard`
- Phase 2 development checkout: `/home/david/Documents/Codex/2026-10-21/insurance-stories-phase-2-architecture-review/stories-dashboard`
- Telegram help worktree: `/tmp/stories-telegram-help` (temporary; do not rely on it surviving cleanup).
- `/srv/stories-dashboard` is the production deployment directory. Its checked-out application source may be older than the running container; inspect the container image to identify the deployed revision.

At the time these instructions were written, GitHub main was `9068bf57edac76c0435545c01fd199baff900fd7` (Telegram story help), following `411b225` (promotion research brief). Fetch GitHub rather than treating these recorded revisions as current.

## Before pushing

From the checkout containing the intended changes:

```bash
cd /home/david/Documents/Codex/2026-10-25/the-promotion-process-doesn-t-have/stories-dashboard
git status --short
git branch -vv
git remote -v
git fetch github main
git log --oneline github/main..HEAD
git log --oneline HEAD..github/main
git diff --stat github/main...HEAD
```

If `github` is missing, add it once:

```bash
git remote add github https://github.com/9dcw/Stories-dashboard.git
```

Review every outgoing commit. Preserve unrelated work in other checkouts. If main has advanced, integrate it on the intended branch before pushing; use `git rebase github/main` only with a clean working tree and unpublished local commits. Resolve conflicts and rerun affected checks. Never force-push main.

For a full release check:

```bash
set -e
python3 -m pytest -q
npm test
node --check app.js
git diff --check
```

The production worker Python environment is `/home/david/.hermes/hermes-agent/venv/bin/python`; it can also run targeted Python tests. Commit the reviewed changes if they are not already committed. Do not create duplicate commits for work already committed in another worktree.

## Authenticated push

Plain HTTPS pushes can fail with `could not read Username` because this environment has no interactive Git credential helper. The existing `GITHUB_TOKEN` binding is configured in `/home/david/.hermes/.env`. Load it with `python-dotenv` and pass it through Git askpass. Never print the token, place it in a remote URL, commit it, or persist it in Git configuration.

Run the following **from the reviewed checkout** after tests pass and a push to main is authorized:

```bash
/home/david/.hermes/hermes-agent/venv/bin/python - <<'PYTHON'
import os
from pathlib import Path
import subprocess
import tempfile
from dotenv import dotenv_values

credential = dotenv_values('/home/david/.hermes/.env').get('GITHUB_TOKEN')
if not credential:
    raise SystemExit('The configured GitHub token is unavailable.')
with tempfile.TemporaryDirectory(prefix='stories-git-askpass-') as directory:
    helper = Path(directory) / 'askpass.py'
    helper.write_text(
        '#!/usr/bin/env python3\n'
        'import os, sys\n'
        'print("x-access-token" if "Username" in sys.argv[1] '
        'else os.environ["GITHUB_TOKEN"])\n'
    )
    helper.chmod(0o700)
    environment = os.environ.copy()
    environment.update(
        GITHUB_TOKEN=credential,
        GIT_ASKPASS=str(helper),
        GIT_TERMINAL_PROMPT='0',
    )
    subprocess.run(['git', 'push', 'github', 'HEAD:main'],
                   env=environment, check=True)
PYTHON
```

This sends the current commit to main with a normal fast-forward push. If a feature branch/PR is required, replace `HEAD:main` with the intended branch ref and follow the repository review process. If Git rejects a push because main advanced, fetch and integrate it; do not force it.

## Verify GitHub and deployment

```bash
git fetch github main
git rev-parse HEAD
git rev-parse github/main
```

Confirm the intended commit appears on [GitHub main](https://github.com/9dcw/Stories-dashboard/commits/main), then watch [GitHub Actions](https://github.com/9dcw/Stories-dashboard/actions).

A push to main starts **Test**. A successful Test run starts **Deploy Contabo**, which waits for approval of the **production** environment. Open that deployment run, select **Review deployments**, select **production**, and approve. The configured token can push but returned HTTP 403, `Resource not accessible by personal access token`, when attempting production approval through the API. User authorization in chat does not expand the token permissions; use the GitHub approval UI. Do not change environment protection rules to bypass this gate.

After deployment, verify the running image and health:

```bash
docker inspect stories-dashboard-app-1 --format '{{.Config.Image}}'
curl -fsS http://127.0.0.1:8000/api/v1/health
```

The image tag should match the deployed commit. A successful push alone does not establish that production has updated.

## Local research worker is a separate deployment

The GitHub workflow deploys the dashboard container; it does not update the separately supervised local worker automatically.

```bash
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user show stories-phase2 \
  -p WorkingDirectory -p ActiveState -p SubState -p NRestarts
XDG_RUNTIME_DIR=/run/user/1000 journalctl --user -u stories-phase2 -n 20 --no-pager
```

Worker code lives under `/srv/stories-dashboard/releases/<commit>/`. Its service is `/home/david/.config/systemd/user/stories-phase2.service`; its configuration is `/home/david/.config/stories/phase2-worker.env`. Do not print credential values from that file.

For a worker change, stage the tested committed source in a new release directory, retain the previous service configuration for rollback, ensure no research step is running, and update both the service WorkingDirectory and ExecStart to the new release. Reload systemd and restart only when worker activation is authorized:

```bash
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user daemon-reload
XDG_RUNTIME_DIR=/run/user/1000 systemctl --user restart stories-phase2
```

Check status and logs after startup has settled. A brief `active` state is insufficient: a failing worker can alternate between running and auto-restart. Google `invalid_grant` means the Google connection needs reauthorization; restarting alone cannot renew revoked credentials. Once renewed, restart and confirm it remains running with no new authentication errors.

The worker was last verified on release `411b22584311a614b0677227bdf7804c1304ddd8`. Telegram help is a separate Hermes plugin installation; a dashboard deployment does not automatically activate that plugin either.

## Execution permissions

This session can require approval for filesystem writes, network access, and commands outside its sandbox. Request the necessary command escalation rather than bypassing the restrictions. No credential values belong in command output, this README, or Git.
