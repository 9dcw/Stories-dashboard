# Contabo deployment

## First-time setup

1. **DNS**: point your domain at Contabo. Verify with `dig A $DOMAIN`.

2. **Server directories**:
   ```bash
   mkdir -p /srv/stories-dashboard/story-data
   mkdir -p /srv/stories-dashboard/backups
   mkdir -p /etc/stories
   ```

3. **Transfer the database** (stop writers first):
   ```bash
   sqlite3 /path/to/stories.sqlite3 ".backup /srv/stories-dashboard/story-data/stories.sqlite3"
   chown -R 1000:1000 /srv/stories-dashboard/story-data
   ```

4. **Environment file** (`/etc/stories/stories.env`):
   ```bash
   STORIES_DB_PATH=/app/story-data/stories.sqlite3
   GOOGLE_CLIENT_ID=YOUR_CLIENT_ID.apps.googleusercontent.com
   # from GCP → APIs & Services → Credentials
   GOOGLE_ALLOWED_EMAIL=you@gmail.com
   GOOGLE_ALLOWED_SUB=1234567890                                  # from your Google account's stable sub claim
   SESSION_SECRET=$(openssl rand -hex 32)                         # generate once, keep safe
   PUBLIC_ORIGIN=https://your-domain.com
   ```

5. **Get your Google subject** (`sub`):
   - Sign in once with the Google button on the login page (temporarily run the app without strict sub matching).
   - Or decode your ID token at https://oauth2.googleapis.com/tokeninfo?id_token=TOKEN
   - Copy the `sub` value — it never changes for your account.

6. **GitHub repo secrets**:
   - Settings → Secrets and variables → Actions
   - Environment: `production`
   - Secrets: `CONTABO_HOST`, `CONTABO_USER`, `CONTABO_SSH_KEY`, `CONTABO_KNOWN_HOSTS`
   - Variables: `DOMAIN`, `DEPLOY_PATH` (optional, defaults to `/srv/stories-dashboard`)

7. **Start**:
   ```bash
   cd /srv/stories-dashboard
   docker compose pull
   docker compose up -d
   ```

## Backups

Run from cron every hour:

```bash
0 * * * * cd /srv/stories-dashboard && python3 scripts/backup_db.py
```

30 generations retained (30 hours at hourly rate).

## Rollback

```bash
cd /srv/stories-dashboard
export IMAGE_TAG=previous-sha
docker compose up -d --no-deps app
```

## Architecture notes

- SQLite is NOT in the image — it is bind-mounted from `/srv/stories-dashboard/story-data/`.
- Polling and CLI continue working directly on the host against the same mounted DB.
- Caddy obtains TLS certificates automatically.
- API mutations and poller writes serialise through SQLite locking.
- Deployment never replaces the database — only the application image changes.