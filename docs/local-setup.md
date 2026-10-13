# Local setup guide

Run AI metrics on your own machine and feed it with your own Claude Code usage and a GitHub repository you control. Takes about 30 minutes.

By the end you will have Grafana showing your Claude Code activity, your pull requests marked as AI-assisted or not, and (optionally) the share of AI-written lines from Git AI.

## What you need

- Docker with Docker Compose
- Git
- Claude Code installed and signed in (`claude` in a terminal)
- A GitHub repository where you can open pull requests

> **Personal Claude accounts (Pro / Max)** cannot use the Claude Code Analytics API — it is only for organizations. Locally, Claude Code data arrives through **OpenTelemetry**, the same path a Team or Enterprise subscription uses in production.

## 1. Get the code

```bash
git clone https://github.com/nosha22/ai-metrics.git
cd ai-metrics
cp .env.example .env
cp config/organization.example.yml config/organization.yml
```

`.env` and `config/organization.yml` are git-ignored on purpose: they hold secrets, names and emails. Never commit them.

## 2. Create a GitHub token

The collector reads pull requests, commits and reviews with a read-only token.

1. Open <https://github.com/settings/personal-access-tokens/new>
   (profile picture → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**).
2. Fill in:
   - **Token name**: `ai-metrics-local`
   - **Resource owner**: your account
   - **Expiration**: 30 days is enough for a test
3. **Repository access**: choose **Only select repositories** and pick your test repository.
4. **Repository permissions**, set to **Read-only**:
   - **Contents** — to clone the repo and read Git AI notes
   - **Pull requests** — to import PRs, commits and reviews
   - **Deployments** — optional, only for the change failure rate

   **Metadata** becomes read-only automatically. Leave everything else at *No access*.
5. Click **Generate token** and copy it straight away (it starts with `github_pat_` and is shown only once).

Check that it works:

```bash
curl -s -H "Authorization: Bearer github_pat_..." \
  "https://api.github.com/repos/YOUR_USER/YOUR_REPO/pulls?state=all" | head -20
```

A list of PRs (or `[]`) means it works. A `404` means the repository was not selected in step 3.

## 3. Fill in `.env`

Open `.env` and set:

```bash
POSTGRES_PASSWORD=any-local-password
GRAFANA_DB_PASSWORD=another-local-password
GRAFANA_ADMIN_PASSWORD=a-third-local-password

GITHUB_TOKEN=github_pat_...

# A secret you make up. Claude Code must send the exact same value.
OTEL_INGEST_TOKEN=local-test-123

# Leave these empty for a personal test; those steps are skipped.
ANTHROPIC_ADMIN_KEY=
CURSOR_ADMIN_KEY=
```

Rules for every value: no quotes, no spaces around `=`, no spaces inside. Use only letters, digits and hyphens for `OTEL_INGEST_TOKEN`.

## 4. Fill in `config/organization.yml`

This file says who is who. Replace its contents with:

```yaml
teams:
  - name: Test

repositories:
  - name: YOUR_USER/YOUR_REPO          # the repo you gave the token
    team: Test

people:
  - name: Your Name
    team: Test
    identities:
      github: YOUR_USER
      git_email:
        - YOUR_COMMIT_EMAIL            # output of: git config user.email
      claude: YOUR_CLAUDE_LOGIN_EMAIL  # the email you sign in to Claude with
    licenses:
      - tool: claude_code
        from: 2026-10-01

detection: {}
```

- **`git_email`**: run `git config user.email` inside the test repo. If GitHub hides your email, commits made on github.com use `12345+YOUR_USER@users.noreply.github.com` — list both.
- **`claude`**: must match your Claude login exactly, or your usage shows up as an unlinked account.

## 5. Start everything

```bash
docker compose up -d --build
curl localhost:8080/health          # → {"ok":true}
```

This starts four containers:

| Container | Port | Role |
| --- | --- | --- |
| `db` | — | Postgres |
| `api` | 8080 | Receives Claude Code telemetry and deploy/incident webhooks |
| `scheduler` | — | Collects GitHub, Claude, Cursor and Git AI data daily at 03:07 UTC, and once on start |
| `grafana` | 3000 | Dashboards |

> **After changing `.env`, run `docker compose up -d`, not `docker compose restart`.** `restart` keeps the old environment; only `up -d` recreates the containers with the new values.

## 6. Send Claude Code telemetry

Add this block to **`~/.claude/settings.json`**, your user settings. A project's `.claude/settings.json` does not work: Claude Code ignores telemetry variables there. If the file already has settings, add `"env"` at the same level, separated by a comma.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "http://localhost:8080/otel/v1/metrics",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer local-test-123"
  }
}
```

- The value after `Bearer` must equal `OTEL_INGEST_TOKEN` in `.env`.
- `OTEL_METRIC_EXPORT_INTERVAL` of 10 000 ms is for testing. Use `600000` (10 minutes) in production.

Claude Code reads this file only at start-up, so **close every Claude Code session and open a new one in a terminal**. The Code tab in the Claude desktop app may not pick up user settings, so test with the `claude` CLI.

First, check that the server accepts your token:

```bash
curl -i -X POST localhost:8080/otel/v1/metrics \
  -H "Authorization: Bearer local-test-123" \
  -H "Content-Type: application/json" -d '{}'
```

You should get `HTTP/1.1 200`.

Then watch the receiver while you use Claude Code:

```bash
docker compose logs -f api
```

Ask Claude Code anything and wait 10–20 seconds. You should see a line like:

```
otel: 6 points, 6 used, 0 without user.email, emails=['you@example.com'], metrics=[...]
```

Confirm the data reached the database:

```bash
docker compose exec db psql -U aimetrics -c \
  "select day, sessions, lines_added, actions_accepted, tokens from ai_daily_activity"
```

## 7. Create a pull request and collect it

1. In your test repo, ask Claude Code for a small change and let it commit. Claude Code adds `Co-Authored-By: Claude <noreply@anthropic.com>` to the commit by default; keep it, because it is what marks the PR as AI-assisted.
2. Push and open a pull request. Merge it so it counts in the dashboards, which show merged PRs.
3. Run the collector now instead of waiting for the nightly run:

```bash
docker compose exec scheduler python -m aimetrics run --days 30
docker compose exec scheduler python -m aimetrics org unlinked
```

`org unlinked` lists accounts that are not mapped to a person. If your email or login appears, fix `organization.yml` and run:

```bash
docker compose exec scheduler python -m aimetrics org sync
docker compose exec scheduler python -m aimetrics attribute --all
```

## 8. Open Grafana

Go to <http://localhost:3000>:

- **Username**: `admin`
- **Password**: the value of `GRAFANA_ADMIN_PASSWORD` in `.env`

Grafana stores the admin password the first time it starts. If you changed `.env` afterwards, or the value was empty then, set it directly:

```bash
docker compose exec grafana grafana cli admin reset-admin-password NewPassword123
```

The dashboards are under **Dashboards → AI**. If you ran an earlier version, an empty **IA** folder may remain; you can delete it.

| Dashboard | What to look for |
| --- | --- |
| **AI · Adoption** | You as an active Claude Code user this week |
| **AI · PR impact** | Your PR as "With AI", and in the table at the bottom the reason (co-author `noreply@anthropic.com`) |
| **AI · Generated code & quality** | Empty until you complete step 9 |

## 9. Optional: Git AI for the share of AI-written lines

Git AI records, line by line, which code an agent wrote. It is installed **globally on the machine where you code** — not inside any project folder.

```bash
# macOS, Linux, Windows with WSL
curl -sSL https://usegitai.com/install.sh | bash
```

```powershell
# Windows without WSL (PowerShell)
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm http://usegitai.com/install.ps1 | iex"
```

Close and reopen your terminal and Claude Code so the agent hooks load, then check with `git ai --version`.

1. In the test repo, ask Claude Code for another change and commit it.
2. Run `git ai stats`. It should show `ai_additions` greater than zero.
3. Push. Git AI sends its notes (`refs/notes/ai`) along with the push.
4. Open and merge a PR, then run `docker compose exec scheduler python -m aimetrics run`.

You do not need to install anything else: the `scheduler` image already includes Git AI to read the notes (`docker compose exec scheduler git-ai --version`).

**If you merge with "Squash and merge"**, GitHub drops the notes on the squashed commit. Copy `deploy/github/git-ai.yaml` from this project to `.github/workflows/git-ai.yaml` **in the test repo** (or run `git ai ci github install` inside it). With a regular merge commit you can skip this.

## Troubleshooting

### No rows in `ai_daily_activity`

Run `docker compose logs -f api` while using Claude Code and match what you see:

| In the `api` log | Cause | Fix |
| --- | --- | --- |
| Nothing | Claude Code is not sending | See [Claude Code is not sending](#claude-code-is-not-sending) |
| `POST /otel/v1/metrics 401` | Token mismatch | See [401 Unauthorized](#401-unauthorized) |
| `POST /otel/v1/metrics 503` | `OTEL_INGEST_TOKEN` is empty in the container | Fill it in `.env`, then `docker compose up -d` |
| `POST /otel/v1/metrics 415` | Wrong protocol | Set `"OTEL_EXPORTER_OTLP_PROTOCOL": "http/json"` |
| `otel: … 0 used, N without user.email` | Data arrives without the user's email | Open an issue with that log line |
| `otel: … used, emails=[...]` | Working | Run the `psql` query again |

### 401 Unauthorized

The value Claude Code sends after `Bearer` is different from the one the container loaded.

1. Check what the container has:
   ```bash
   docker compose exec api printenv OTEL_INGEST_TOKEN
   ```
   If it is empty or old, `.env` was not reloaded: run `docker compose up -d` (not `restart`). Make sure `.env` sits next to `docker-compose.yml` and is named exactly `.env`.
2. Compare it with `OTEL_EXPORTER_OTLP_HEADERS` in `~/.claude/settings.json`, character by character.
3. Run `docker compose logs api | grep 401`. The log says whether the header was missing, or shows the length and first two characters of each token (never the token itself).
4. Start a new Claude Code session after any change to `settings.json`.

### Claude Code is not sending

Start Claude Code with a debug log, ask something, wait 20 seconds, then search the log:

```bash
claude --debug-file ~/cc-debug.log
grep -iE "3P telemetry|otel" ~/cc-debug.log | tail -20
```

Common causes:

- The `env` block is in a project's `.claude/settings.json` instead of `~/.claude/settings.json`.
- `settings.json` has a JSON syntax error, often a missing comma between `env` and another setting.
- You are using the Claude desktop app instead of the `claude` CLI.
- Claude Code was already running when you edited `settings.json`.

### The PR is not marked as AI-assisted

- The commit must contain `Co-Authored-By: Claude <noreply@anthropic.com>`. Check with `git log -1`.
- The dashboards show **merged** PRs within the selected time range only.
- Run `docker compose exec scheduler python -m aimetrics attribute --all` after fixing `organization.yml`.

### The collector fails on GitHub

Run `docker compose exec scheduler python -m aimetrics sync github --days 30` and read the error:

- `401`: the token is wrong or expired.
- `404`: the repository is not selected in the token, or the name in `organization.yml` is wrong (format `owner/repo`).

## Clean up

When you are done testing:

1. Remove the `env` block from `~/.claude/settings.json`; otherwise Claude Code keeps trying to send telemetry to `localhost`.
2. Stop the containers. Add `-v` to also delete the database and Grafana data:
   ```bash
   docker compose down        # keeps data
   docker compose down -v     # deletes everything
   ```
3. Delete the GitHub token at <https://github.com/settings/personal-access-tokens> if you no longer need it.
