# Server setup (no public IP, with Cloudflare Tunnel)

Install AI metrics on a Linux server that only has outbound internet access, such as our Scaleway machine.
Cloudflare Tunnel publishes Grafana and the telemetry endpoint without opening any inbound port.

```
developers' laptops ──HTTPS──► Cloudflare ◄──outbound tunnel── cloudflared ─┬─► api (:8080)
                                                                            └─► grafana (:3000)
```

## What you need

- SSH access to the server, with Docker and the Docker Compose plugin (`docker compose version`)
- About 2 GB of free RAM and 10 GB of disk
- A domain managed in Cloudflare, and access to Cloudflare Zero Trust (the free plan is enough)
- A read-only GitHub token for the organization's repositories (see [local-setup.md](local-setup.md#2-create-a-github-token))

## 1. Get the code and configure it

```bash
git clone https://github.com/nosha22/ai-metrics.git
cd ai-metrics
cp .env.example .env
cp config/organization.example.yml config/organization.yml
```

Generate strong secrets and keep them in the password manager:

```bash
for v in POSTGRES_PASSWORD GRAFANA_DB_PASSWORD GRAFANA_ADMIN_PASSWORD OTEL_INGEST_TOKEN EVENTS_TOKEN; do
  echo "$v=$(openssl rand -hex 24)"
done
```

Fill in `.env` with those values, plus:

```bash
GITHUB_TOKEN=github_pat_...
CURSOR_ADMIN_KEY=            # optional, needs a Cursor team admin
ANTHROPIC_ADMIN_KEY=         # optional, only for Claude Console (API billing)

# Nothing listens on the server's interfaces; only the tunnel reaches the services
API_PORT=127.0.0.1:8080
GRAFANA_PORT=127.0.0.1:3000
```

Fill in `config/organization.yml` with teams, people (GitHub login, commit emails, Claude and Cursor emails) and repositories.

## 2. Create the Cloudflare Tunnel

1. In the Cloudflare dashboard, go to **Zero Trust → Networks → Tunnels → Create a tunnel**, type **Cloudflared**, name it `ai-metrics`.
2. On the install step, copy the token (the long string after `--token`) and add it to `.env`:
   ```bash
   CLOUDFLARE_TUNNEL_TOKEN=eyJ...
   ```
   You do not need to install cloudflared on the server; it runs as a container.
3. Add two **public hostnames** to the tunnel:

   | Hostname | Service |
   | --- | --- |
   | `ai-metrics.<your-domain>` | `http://grafana:3000` |
   | `ai-metrics-ingest.<your-domain>` | `http://api:8080` |

## 3. Start everything

```bash
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d --build
docker compose ps                                   # all five services should be "running"
docker compose logs cloudflared | grep -i registered # the tunnel is connected
curl -s https://ai-metrics-ingest.<your-domain>/health   # → {"ok":true}
```

Tip: to avoid typing both files every time, add `COMPOSE_FILE=docker-compose.yml:docker-compose.tunnel.yml` to `.env`.
After that, plain `docker compose ...` commands use both.

## 4. Protect Grafana with Cloudflare Access

1. **Zero Trust → Access → Applications → Add an application → Self-hosted**.
2. Domain: `ai-metrics.<your-domain>`. Policy: allow emails ending in the company domain (or a specific group).
3. Do **not** add `ai-metrics-ingest.<your-domain>` to Access. Claude Code cannot log in; that hostname is protected by `OTEL_INGEST_TOKEN`.

Grafana also has its own login (`admin` / `GRAFANA_ADMIN_PASSWORD`). Create viewer accounts for colleagues under
**Administration → Users**, or keep only the admin account and rely on Access.

## 5. Connect the team

- **Claude Code**: copy `deploy/claude-code/managed-settings.json`, set the endpoint to
  `https://ai-metrics-ingest.<your-domain>/otel/v1/metrics` and the token to `OTEL_INGEST_TOKEN`, and paste it into the
  Claude Code managed settings in the Claude admin console (Team/Enterprise). Each developer starts a new Claude Code session.
- **Git AI**: each developer runs `curl -sSL https://usegitai.com/install.sh | bash` once.
- **Squash merges**: add `deploy/github/git-ai.yaml` as `.github/workflows/git-ai.yaml` in each measured repository.

## 6. First collection and checks

```bash
docker compose exec scheduler python -m aimetrics run --days 90
docker compose exec scheduler python -m aimetrics org unlinked
```

After that, the `scheduler` collects automatically every night at 03:07 UTC. Open `https://ai-metrics.<your-domain>`,
folder **AI**, to see the dashboards.

## Maintenance

| Task | Command |
| --- | --- |
| Update to the latest version | `git pull && docker compose up -d --build` |
| Apply changes to `.env` | `docker compose up -d` (not `restart`) |
| Apply changes to `organization.yml` | `docker compose exec scheduler python -m aimetrics org sync` |
| Logs | `docker compose logs -f api scheduler cloudflared` |
| Database backup | `docker compose exec -T db pg_dump -U aimetrics aimetrics \| gzip > backup-$(date +%F).sql.gz` |

Add the backup command to the server's crontab and copy the files off the server (e.g. to object storage).
