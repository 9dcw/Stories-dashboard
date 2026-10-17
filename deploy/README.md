# Contabo deployment

This guide assumes one Contabo VPS, Docker Compose, Caddy, and SQLite. Commands marked **server** run after logging into Contabo. Browser steps run on GitHub or Google Cloud.

## Values used in the examples

```bash
CONTABO_HOST=86.48.21.252
CONTABO_USER=david
DOMAIN=vmi3298003.contaboserver.net
DEPLOY_PATH=/srv/stories-dashboard
SOURCE_DB=/home/david/.hermes/work/stories-dashboard/story-data/stories.sqlite3
```

`DOMAIN` is the hostname typed into a browser, without `https://` and without a path. `PUBLIC_ORIGIN` is the same hostname with `https://` in front.

## 1. Domain and DNS

“Point the domain at Contabo” means: create a public DNS **A** record whose value is the Contabo IPv4 address (`86.48.21.252`). There are two choices:

- **Contabo hostname:** use `vmi3298003.contaboserver.net`. Contabo already publishes its A record, so no DNS change is required.
- **Custom hostname:** for a domain you own, such as `example.com`, open the DNS page at its registrar/DNS provider and add:
  - Type: `A`
  - Name/Host: `stories`
  - Value/Points to: `86.48.21.252`
  - TTL: `300` or `Auto`

  This produces `stories.example.com`. Do not enter `https://` in a DNS field.

Verify from any computer with `dig`:

```bash
dig +short A "$DOMAIN"
```

It must return `86.48.21.252`. If a custom hostname has an `AAAA` record but the server is not configured for that IPv6 address, remove that conflicting record.

## 2. Checkout and persistent directories

The production deployment must eventually track `main`. During pre-merge setup, replace `main` below with the feature branch explicitly.

**Server:**

```bash
sudo mkdir -p /srv/stories-dashboard /etc/stories
sudo chown "$USER:$USER" /srv/stories-dashboard
sudo chmod 0755 /srv/stories-dashboard
sudo chown root:"$USER" /etc/stories
sudo chmod 0750 /etc/stories

git clone --branch main --single-branch \
  https://github.com/9dcw/Stories-dashboard.git \
  /srv/stories-dashboard

mkdir -p /srv/stories-dashboard/story-data
mkdir -p /srv/stories-dashboard/backups
chmod 0755 /srv/stories-dashboard/story-data /srv/stories-dashboard/backups
```

Verify:

```bash
git -C /srv/stories-dashboard status --short --branch
stat -c '%n %U:%G %a' \
  /srv/stories-dashboard \
  /srv/stories-dashboard/story-data \
  /srv/stories-dashboard/backups \
  /etc/stories
```

## 3. Transfer the SQLite database

`SOURCE_DB` means the current authoritative database. Stop or pause anything that writes to it, then use Python's built-in SQLite backup API. This works even when the separate `sqlite3` shell command is not installed.

**Server:**

```bash
export SOURCE_DB=/path/to/current/stories.sqlite3
export DEST_DB=/srv/stories-dashboard/story-data/stories.sqlite3

python3 - "$SOURCE_DB" "$DEST_DB" <<'PY'
import os
import sqlite3
import sys

source_path, destination_path = sys.argv[1:]
temporary_path = destination_path + ".incoming"
if os.path.exists(temporary_path):
    os.unlink(temporary_path)
source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
destination = sqlite3.connect(temporary_path)
try:
    source.backup(destination)
finally:
    destination.close()
    source.close()
os.chmod(temporary_path, 0o600)
os.replace(temporary_path, destination_path)
PY

chown 1000:1000 "$DEST_DB"
python3 - "$DEST_DB" <<'PY'
import sqlite3
import sys
connection = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
print(connection.execute("PRAGMA quick_check").fetchone()[0])
PY
```

The last command must print `ok`. Resume writers only after the backup finishes.

## 4. Create a Google OAuth Web client

This application uses Google Identity Services in a browser. A **Desktop app** OAuth client is not sufficient; create a **Web application** client.

1. Open <https://console.cloud.google.com/apis/credentials>.
2. Select an existing project or create one named `Stories Dashboard`.
3. If prompted, configure the OAuth consent screen. If it is in Testing, add the allowed Google account as a test user.
4. Click **Create credentials → OAuth client ID**.
5. Application type: **Web application**.
6. Under **Authorized JavaScript origins**, add exactly `https://vmi3298003.contaboserver.net` (or the chosen `PUBLIC_ORIGIN`).
7. No redirect URI is required for the Google Identity Services credential flow used by this app.
8. Copy the client ID ending in `.apps.googleusercontent.com`. The app does not use or need the client secret.

## 5. Obtain the Google account `sub`

`sub` is Google's immutable numeric identifier for the allowed account. It is not the email address. Do not paste an ID token into Git, chat, or the env file.

One no-code method:

1. Open <https://developers.google.com/oauthplayground>.
2. In Step 1, enter the scopes `openid email` and authorize them.
3. Sign in as the exact account that will be allowed into the dashboard.
4. In Step 2, exchange the authorization code for tokens.
5. Copy the temporary `id_token` only into Google's token-info URL: `https://oauth2.googleapis.com/tokeninfo?id_token=TOKEN`.
6. Copy only the JSON field named `sub`. The temporary ID token itself is a credential and should not be saved.
7. Confirm the `email` field is the account you intended to allow.

## 6. Create `/etc/stories/stories.env`

Generate the session secret on the server. Keep it on the server and do not paste it into GitHub or chat.

**Server:**

```bash
openssl rand -hex 32
```

Copy the 64-character output. Then create the file:

```bash
sudoedit /etc/stories/stories.env
```

Paste this, replacing the three `REPLACE_...` values:

```dotenv
STORIES_DB_PATH=/app/story-data/stories.sqlite3
GOOGLE_CLIENT_ID=REPLACE_WITH_WEB_CLIENT_ID.apps.googleusercontent.com
GOOGLE_ALLOWED_EMAIL=davecwright@gmail.com
GOOGLE_ALLOWED_SUB=REPLACE_WITH_NUMERIC_GOOGLE_SUB
SESSION_SECRET=REPLACE_WITH_THE_GENERATED_64_HEX_CHARACTERS
PUBLIC_ORIGIN=https://vmi3298003.contaboserver.net
```

Then lock down and verify the file without printing secret values:

```bash
sudo chown root:"$USER" /etc/stories/stories.env
sudo chmod 0640 /etc/stories/stories.env
sudo awk -F= 'NF >= 2 {print $1 "=SET"}' /etc/stories/stories.env
```

The deployment user needs read access because Docker Compose reads `env_file`
before starting the container. Other users still have no access.

`DOMAIN` is deliberately not required in this secrets file. It is a non-secret Docker Compose/GitHub Actions deployment variable.

## 7. Configure the GitHub production deployment

Create a dedicated SSH key for GitHub Actions; do not use a personal/root key. Install its public key in the deployment user's `~/.ssh/authorized_keys` and verify that key-only login works before putting the private key into GitHub.

Pin the host-key algorithm that the deployment client actually negotiates. `appleboy/ssh-action@v1` currently negotiates this server's ECDSA host key, so derive the trusted value on the server:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ecdsa_key.pub -E sha256
```

From a second trusted network, confirm the same verified ECDSA fingerprint is presented publicly:

```bash
ssh-keyscan -t ecdsa 86.48.21.252 2>/dev/null | ssh-keygen -lf - -E sha256
```

Do not substitute the ED25519 fingerprint merely because ED25519 is generally preferred; the pin must match the host-key algorithm selected by this deployment client.

In GitHub, open `Settings → Environments → production`.

Add these **environment secrets**:

- `CONTABO_HOST`: `86.48.21.252`
- `CONTABO_USER`: `david`
- `CONTABO_SSH_KEY`: the complete dedicated private key, including its BEGIN and END lines
- `CONTABO_HOST_FINGERPRINT`: the verified ECDSA fingerprint selected by the deployment client, beginning with `SHA256:`

Add these **environment variables**:

- `DOMAIN`: `vmi3298003.contaboserver.net`
- `DEPLOY_PATH`: `/srv/stories-dashboard`

The GitHub token used by automation needs repository permissions **Secrets: read/write**, **Variables: read/write**, and **Environments: read/write** to do this through the API. Ordinary Git push permission is not enough.

## 8. First start

Do not start until DNS, the database, the complete env file, and ports 80/443 are ready.

**Server:**

```bash
cd /srv/stories-dashboard
export DOMAIN=vmi3298003.contaboserver.net
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

Verify publicly:

```bash
curl --fail "https://$DOMAIN/api/v1/health"
```

Open `https://vmi3298003.contaboserver.net`, sign in with the allowed Google account, dismiss one test story, undo it, and confirm the state survives a page refresh.

## Backups

Run from cron every hour:

```cron
0 * * * * cd /srv/stories-dashboard && python3 scripts/backup_db.py
```

Thirty generations are retained (30 hours at an hourly rate). Also keep an off-server backup; snapshots stored only on the VPS do not protect against loss of the VPS.

## Rollback

```bash
cd /srv/stories-dashboard
export DOMAIN=vmi3298003.contaboserver.net
STORIES_IMAGE=ghcr.io/9dcw/stories-dashboard:previous-sha \
  docker compose up -d --no-deps app
```

## Architecture notes

- SQLite is not baked into the image; it is bind-mounted from `/srv/stories-dashboard/story-data/`.
- Polling and CLI operations use the same database on the host.
- Caddy obtains and renews TLS certificates automatically.
- API mutations and poller writes serialize through SQLite locking.
- Deployment changes the application image and never intentionally replaces the database.
