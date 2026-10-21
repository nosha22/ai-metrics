# AI metrics — Outsmart

Swarmia-style metrics for AI adoption and impact in Outsmart engineering, self-hosted on our own server.

It answers four questions:

| Question | Dashboard | Source |
| --- | --- | --- |
| Who uses the AI tools? | **AI · Adoption** | Claude Code (Analytics API or OpenTelemetry), Cursor Admin API, `organization.yml` |
| How do they use them? | **AI · Adoption** → Activity | same as above |
| What effect do they have on pull requests? | **AI · PR impact** | GitHub (PRs, commits, reviews) + detection rules |
| How much of the code is AI-written, and does it hold up? | **AI · Generated code & quality** | Git AI notes, `git blame`, deployments and incidents |

The detection rules and metric definitions follow Swarmia's public documentation
([detection](https://help.swarmia.com/features/ai-tools/ai-tool-detection-and-filters),
[adoption](https://help.swarmia.com/features/ai-tools/ai-adoption)). They live in `aimetrics/detection.py` and `db/views.sql`.

## Architecture

```
GitHub API ─┐
Claude API ─┤                     ┌──────────────┐
Cursor API ─┼─► scheduler (daily) ─►             │
Git AI     ─┘                     │   Postgres   ├──► Grafana (3 dashboards)
Claude Code ─OTLP─► api (:8080) ──►  facts +     │
CI deploys ──webhook─► api ───────►   views      │
                                  └──────────────┘
```

* `scheduler` runs `aimetrics run` every day at 03:07 UTC (and once on start-up).
* `api` receives Claude Code telemetry and deployment/incident events.
* Sources only store raw facts; attribution and metrics are computed on top of them. After changing a rule, `aimetrics attribute --all` recalculates the whole history.

## Try it locally

Step-by-step guide to run everything on your machine with your own Claude account and one of your repositories:
[docs/local-setup.md](docs/local-setup.md).

## Server installation

For a server without a public IP (Cloudflare Tunnel + Access), follow [docs/server-setup.md](docs/server-setup.md).
The quick version for a server that already has HTTPS in front of it:

```bash
git clone https://github.com/nosha22/ai-metrics.git && cd ai-metrics
cp .env.example .env                                         # fill in (keep values in the password manager)
cp config/organization.example.yml config/organization.yml   # teams, people, repositories
docker compose up -d --build
```

Grafana runs on `http://<server>:3000` (user `admin`, password from `GRAFANA_ADMIN_PASSWORD`), folder **AI**.
Put Grafana and port 8080 behind a reverse proxy with HTTPS before connecting OpenTelemetry. To keep both ports
off the internet, set `API_PORT=127.0.0.1:8080` and `GRAFANA_PORT=127.0.0.1:3000` in `.env`.

Useful commands (inside the `scheduler` container):

```bash
docker compose exec scheduler python -m aimetrics run                     # full collection now
docker compose exec scheduler python -m aimetrics sync github --days 180  # backfill
docker compose exec scheduler python -m aimetrics org unlinked            # accounts not mapped to a person
docker compose exec scheduler python -m aimetrics attribute --all
```

## Connecting the sources

### Phase 1 — GitHub (required)

A **read-only** fine-grained token on the repositories to measure: *Pull requests*, *Contents*, *Deployments*, *Metadata*.
Put it in `GITHUB_TOKEN` and list the repositories in `organization.yml`.

For the high-confidence rules to work, the tools must keep their attribution in commits:

* **Claude Code**: do not disable the [attribution settings](https://code.claude.com/docs/en/settings#attribution-settings)
  (`Co-Authored-By: Claude <noreply@anthropic.com>`).
* **Cursor**: keep the `Made-with: Cursor` trailer.

### Phase 2 — Claude Code and Cursor

**Claude Code** — depends on how the team pays:

| Plan | How to connect |
| --- | --- |
| Claude Console (API billing) | Admin API key in `ANTHROPIC_ADMIN_KEY`. Includes 30 days of history. |
| Team / Enterprise (subscription) | OpenTelemetry: copy `deploy/claude-code/managed-settings.json` into the managed settings on claude.ai (admin → Claude Code), with the public URL of `api` and the `OTEL_INGEST_TOKEN`. No history; does not show idle seats. |

Both can run side by side; when the same person appears in both on the same day, the source with more usage wins.
The receiver accepts only `http/json` with `delta` temporality (it also converts `cumulative`).

**Cursor** — Admin API key in `CURSOR_ADMIN_KEY`. Members of the Cursor team become the licenses
(use `--no-licenses` to manage licenses only in the YAML).

After connecting, run `aimetrics org unlinked` and add any missing accounts to `organization.yml`.
Identical emails across sources are linked automatically.

### Phase 3 — Git AI (share of AI code)

1. Install Git AI on the team's machines: `curl -sSL https://usegitai.com/install.sh | bash`
   (without a telemetry key: attribution stays in git notes only).
2. In each repository, add `deploy/github/git-ai.yaml` as `.github/workflows/git-ai.yaml`
   (or run `git ai ci github install`). Without it, GitHub's *squash merge* drops the notes.
3. The `scheduler` clones the repositories (no checkout), reads `refs/notes/ai` with `git ai stats <sha> --json`
   and measures each PR's line survival with `git blame`, once a week, for 180 days.

The dashboard shows the **coverage** (commits with a note ÷ commits). Below ~80% the AI share is not representative.

### Phase 4 — Deployments and incidents (change failure rate)

* **Deployments**: if you use GitHub's Deployments API, the collector imports them on its own. Otherwise, add
  `deploy/github/report-deployment.yaml` to the deployment workflow.
* **Incidents**: `POST /events/incident` with `EVENTS_TOKEN`:

```bash
curl -X POST https://<api>/events/incident -H "Authorization: Bearer $EVENTS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"INC-42","title":"Login fails","opened_at":"2026-10-13T10:00:00Z","deployment_id":"nosha22/outsmart-api-123"}'
```

## Detection rules

| Mode | Rule | Confidence |
| --- | --- | --- |
| Assisted | Commit authored or co-authored by AI (`Co-Authored-By`, commit author) | High |
| Assisted | Tool trailer (`Made-with: Cursor`, `Claude-Session:`) | High |
| Assisted | `claude-code-assisted` label on the PR | High |
| Assisted | Git AI recorded AI lines in the PR's commits | High |
| Assisted | A commit's author used the tool in the previous 24 hours | Low |
| Agent | PR or first commit created by an agent (`claude[bot]`, `cursoragent`, …) | High |
| AI review | Review by `claude[bot]`, `cursor[bot]`, … | High |

The **High confidence only** filter in the dashboards turns off the 24-hour rule.
Extra patterns go under `detection:` in `organization.yml`. Each PR stores the reason for its attribution.

## Development

```bash
pip install -e ".[dev]"
pytest -q                                            # unit tests
TEST_DATABASE_URL=postgresql://... pytest -q          # + end-to-end test against Postgres
python scripts/build_dashboards.py                   # dashboards are generated from this script
```

## Privacy

* Dashboards show teams; per-person data appears only in the idle-seats table and the PR details table.
* No prompt content or code is stored: only counts, dates and commit/PR metadata.
* Before connecting the sources, tell the team what is collected and why.
