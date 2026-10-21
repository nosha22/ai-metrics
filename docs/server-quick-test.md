# Quick test on the Scaleway server (no domain needed)

Run AI metrics on the shared server and send your own Claude Code usage to it, without a domain or a Cloudflare account.
The telemetry endpoint is published through a free Cloudflare **Quick Tunnel** (`https://<random>.trycloudflare.com`);
Grafana stays private and you reach it through SSH.

Time needed: about 45 minutes.

> Quick Tunnels are for testing only: the address changes whenever the tunnel restarts, and it cannot be protected
> with Cloudflare Access. For the team setup, use [server-setup.md](server-setup.md).

## Before you start

- [ ] SSH access to the server, and permission to run Docker (`docker ps` works without errors)
- [ ] A read-only GitHub fine-grained token for the test repository (see [local-setup.md](local-setup.md#2-create-a-github-token))
- [ ] The email you sign in to Claude with, and the email in your commits (`git config user.email`)
- [ ] Claude Code installed on your laptop (`claude` in a terminal)

## 1. Check the server

```bash
docker compose version     # Docker Compose plugin is installed
free -h                    # at least ~2 GB available
df -h /                    # at least ~10 GB free
ss -ltn | grep -E ':(3000|8080) '   # ports in use; if anything shows, see the note below
```

If port 3000 or 8080 is already taken on the server, pick other ports in step 2 (for example `127.0.0.1:3001` and
`127.0.0.1:8081`) and use them everywhere below.

## 2. Get the code and configure it

```bash
git clone https://github.com/nosha22/ai-metrics.git
cd ai-metrics
cp .env.example .env
cp config/organization.example.yml config/organization.yml
```

Generate secrets:

```bash
for v in POSTGRES_PASSWORD GRAFANA_DB_PASSWORD GRAFANA_ADMIN_PASSWORD OTEL_INGEST_TOKEN EVENTS_TOKEN; do
  echo "$v=$(openssl rand -hex 24)"
done
```

Edit `.env` (`nano .env`) and set:

```bash
POSTGRES_PASSWORD=<generated>
GRAFANA_DB_PASSWORD=<generated>
GRAFANA_ADMIN_PASSWORD=<generated>
OTEL_INGEST_TOKEN=<generated>
EVENTS_TOKEN=<generated>
GITHUB_TOKEN=github_pat_...

# Keep both services private to the server
API_PORT=127.0.0.1:8080
GRAFANA_PORT=127.0.0.1:3000
```

Leave `CLOUDFLARE_TUNNEL_TOKEN` and `COMPOSE_FILE` commented out. Save the generated values in the password manager.

Replace the contents of `config/organization.yml` (`nano config/organization.yml`):

```yaml
teams:
  - name: Test

repositories:
  - name: OWNER/REPO                 # the repository the token can read
    team: Test

people:
  - name: Your Name
    team: Test
    identities:
      github: YOUR_GITHUB_LOGIN
      git_email:
        - YOUR_COMMIT_EMAIL
      claude: YOUR_CLAUDE_LOGIN_EMAIL
    licenses:
      - tool: claude_code
        from: 2026-10-01

detection: {}
```

## 3. Start the stack

```bash
docker compose up -d --build        # first build takes a few minutes
docker compose ps                   # db, api, scheduler and grafana are "running"
curl -s localhost:8080/health       # → {"ok":true}
```

## 4. Publish the telemetry endpoint with a Quick Tunnel

```bash
docker run -d --name ai-metrics-quicktunnel --restart unless-stopped --network host \
  cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://localhost:8080
sleep 10
docker logs ai-metrics-quicktunnel 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
```

Write the address down, then check it from your laptop:

```bash
curl -s https://<address>.trycloudflare.com/health    # → {"ok":true}
```

## 5. Point your Claude Code at the server

On your laptop, edit `~/.claude/settings.json` (user settings, not a project's `.claude/` folder).
If the file already has settings, add `"env"` at the same level, separated by a comma.

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "https://<address>.trycloudflare.com/otel/v1/metrics",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer <OTEL_INGEST_TOKEN from .env>"
  }
}
```

Close every Claude Code session and open a new one in a terminal. Ask it anything, then on the server:

```bash
docker compose logs --tail 20 api | grep otel
# → otel: N points, N used, 0 without user.email, emails=['you@...']
```

## 6. Create a test pull request and collect it

1. In the test repository, ask Claude Code for a small change and let it commit (keep the
   `Co-Authored-By: Claude <noreply@anthropic.com>` line).
2. Push, open a pull request and merge it.
3. On the server:
   ```bash
   docker compose exec scheduler python -m aimetrics run --days 30
   docker compose exec scheduler python -m aimetrics org unlinked
   ```
   If `org unlinked` lists your email or login, fix `organization.yml` and run
   `docker compose exec scheduler python -m aimetrics org sync` and then `... attribute --all`.

## 7. Open Grafana through SSH

From your laptop, add a port forward to the way you usually connect:

```bash
ssh -L 3001:localhost:3000 <your-usual-ssh-target>
```

Open `http://localhost:3001` (user `admin`, password `GRAFANA_ADMIN_PASSWORD`), then **Dashboards → AI**:

- **AI · Adoption**: you as an active Claude Code user this week
- **AI · PR impact**: your PR as "With AI", and the reason in the table at the bottom

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `docker compose up` fails with "set it in .env" | A required value in `.env` is empty |
| `POST /otel/v1/metrics 401` in the `api` log | Token in `settings.json` differs from `OTEL_INGEST_TOKEN`; after editing `.env`, run `docker compose up -d` (not `restart`) |
| Nothing in the `api` log | Claude Code was not restarted, or the `env` block is in the wrong file; see [local-setup.md](local-setup.md#claude-code-is-not-sending) |
| The trycloudflare address stopped working | The tunnel restarted and got a new address: run the `docker logs … grep` command again and update `settings.json` |
| Collector fails on GitHub with 404 | The repository is not selected in the token, or the name in `organization.yml` is wrong (`owner/repo`) |

## When you're done testing

- On your laptop, remove the `env` block from `~/.claude/settings.json`.
- On the server, stop the quick tunnel: `docker rm -f ai-metrics-quicktunnel`.
- Keep the stack running and move to the permanent setup with a domain and Cloudflare Access: [server-setup.md](server-setup.md).
