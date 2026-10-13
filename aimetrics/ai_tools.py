"""Ingestion from the admin APIs: Claude Code Analytics API and Cursor Admin API."""
from __future__ import annotations

import datetime as dt
import logging

import httpx

from . import db

log = logging.getLogger(__name__)

UPSERT_ACTIVITY = """
INSERT INTO ai_daily_activity (identity_id, tool, day, source, is_active, sessions, lines_added, lines_removed,
    actions_accepted, actions_rejected, chat_requests, agent_requests, commits, pull_requests, tokens, cost_usd)
VALUES (%(identity_id)s, %(tool)s, %(day)s, %(source)s, %(is_active)s, %(sessions)s, %(lines_added)s, %(lines_removed)s,
    %(actions_accepted)s, %(actions_rejected)s, %(chat_requests)s, %(agent_requests)s, %(commits)s, %(pull_requests)s,
    %(tokens)s, %(cost_usd)s)
ON CONFLICT (identity_id, tool, day, source) DO UPDATE SET
    is_active=EXCLUDED.is_active, sessions=EXCLUDED.sessions, lines_added=EXCLUDED.lines_added,
    lines_removed=EXCLUDED.lines_removed, actions_accepted=EXCLUDED.actions_accepted,
    actions_rejected=EXCLUDED.actions_rejected, chat_requests=EXCLUDED.chat_requests,
    agent_requests=EXCLUDED.agent_requests, commits=EXCLUDED.commits, pull_requests=EXCLUDED.pull_requests,
    tokens=EXCLUDED.tokens, cost_usd=EXCLUDED.cost_usd
"""

# ---------------------------------------------------------------------------
# Claude Code Analytics API (contas Claude Console / API)
# https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
# ---------------------------------------------------------------------------
CLAUDE_URL = "https://api.anthropic.com/v1/organizations/usage_report/claude_code"


def claude_record_to_activity(rec: dict) -> tuple[str, str, dict]:
    """Converts an API record into (identity source, value, activity row)."""
    actor = rec.get("actor") or {}
    if actor.get("type") == "user_actor":
        source, value = "claude", actor.get("email_address")
    else:
        source, value = "claude_api_key", actor.get("api_key_name")
    core = rec.get("core_metrics") or {}
    loc = core.get("lines_of_code") or {}
    tools = rec.get("tool_actions") or {}
    accepted = sum((t or {}).get("accepted", 0) for t in tools.values())
    rejected = sum((t or {}).get("rejected", 0) for t in tools.values())
    tokens = cost_cents = 0
    for m in rec.get("model_breakdown") or []:
        tk = m.get("tokens") or {}
        tokens += sum(tk.get(k, 0) for k in ("input", "output", "cache_read", "cache_creation"))
        cost_cents += (m.get("estimated_cost") or {}).get("amount", 0)  # in US cents
    sessions = core.get("num_sessions", 0)
    return source, value, {
        "tool": "claude_code",
        "day": rec["date"][:10],
        "source": "analytics_api",
        # Swarmia: active = accepted code or used chat; in a Claude Code session, chatting is the usage itself
        "is_active": accepted > 0 or sessions > 0,
        "sessions": sessions,
        "lines_added": loc.get("added", 0),
        "lines_removed": loc.get("removed", 0),
        "actions_accepted": accepted,
        "actions_rejected": rejected,
        "chat_requests": 0,
        "agent_requests": 0,
        "commits": core.get("commits_by_claude_code", 0),
        "pull_requests": core.get("pull_requests_by_claude_code", 0),
        "tokens": tokens,
        "cost_usd": cost_cents / 100,
    }


def sync_claude(conn, admin_key: str, days: int, client: httpx.Client | None = None) -> int:
    client = client or httpx.Client(timeout=30)
    headers = {"x-api-key": admin_key, "anthropic-version": "2023-06-01", "User-Agent": "outsmart-ai-metrics/1.0"}
    today = dt.datetime.now(dt.timezone.utc).date()
    rows = 0
    for offset in range(days, -1, -1):
        day = today - dt.timedelta(days=offset)
        page = None
        while True:
            params = {"starting_at": day.isoformat(), "limit": 1000}
            if page:
                params["page"] = page
            r = client.get(CLAUDE_URL, params=params, headers=headers)
            r.raise_for_status()
            body = r.json()
            for rec in body.get("data", []):
                source, value, act = claude_record_to_activity(rec)
                ident = db.get_or_create_identity(conn, source, value)
                if ident:
                    conn.execute(UPSERT_ACTIVITY, {"identity_id": ident, **act})
                    rows += 1
            if not body.get("has_more"):
                break
            page = body.get("next_page")
    conn.commit()
    log.info("claude analytics api: %d linhas", rows)
    return rows


# ---------------------------------------------------------------------------
# Cursor Admin API — https://docs.cursor.com/account/teams/admin-api
# ---------------------------------------------------------------------------
CURSOR_API = "https://api.cursor.com"


def cursor_record_to_activity(rec: dict) -> dict:
    accepts = rec.get("totalAccepts", 0) + rec.get("totalTabsAccepted", 0)
    chats = rec.get("chatRequests", 0) + rec.get("composerRequests", 0) + rec.get("cmdkUsages", 0)
    agent = rec.get("agentRequests", 0)
    return {
        "tool": "cursor",
        "day": dt.datetime.fromtimestamp(rec["date"] / 1000, dt.timezone.utc).date().isoformat(),
        "source": "cursor_api",
        "is_active": bool(rec.get("isActive")) and (accepts > 0 or chats > 0 or agent > 0),
        "sessions": 0,
        "lines_added": rec.get("acceptedLinesAdded", 0),
        "lines_removed": rec.get("acceptedLinesDeleted", 0),
        "actions_accepted": accepts,
        "actions_rejected": rec.get("totalRejects", 0),
        "chat_requests": chats,
        "agent_requests": agent,
        "commits": 0,
        "pull_requests": 0,
        "tokens": 0,
        "cost_usd": 0,
    }


def sync_cursor(conn, admin_key: str, days: int, manage_licenses: bool = True, client: httpx.Client | None = None) -> int:
    client = client or httpx.Client(base_url=CURSOR_API, timeout=30, auth=(admin_key, ""))
    now = dt.datetime.now(dt.timezone.utc)
    rows = 0
    start = now - dt.timedelta(days=days)
    while start < now:  # the API accepts at most 90 days per request
        end = min(start + dt.timedelta(days=89), now)
        r = client.post("/teams/daily-usage-data", json={"startDate": int(start.timestamp() * 1000), "endDate": int(end.timestamp() * 1000)})
        r.raise_for_status()
        for rec in r.json().get("data", []):
            ident = db.get_or_create_identity(conn, "cursor", rec.get("email"))
            if ident:
                conn.execute(UPSERT_ACTIVITY, {"identity_id": ident, **cursor_record_to_activity(rec)})
                rows += 1
        start = end
    if manage_licenses:
        members = client.get("/teams/members").json().get("teamMembers", [])
        sync_cursor_licenses(conn, [m["email"] for m in members if m.get("email")], now.date())
    conn.commit()
    log.info("cursor api: %d linhas", rows)
    return rows


def sync_cursor_licenses(conn, emails: list[str], today: dt.date) -> None:
    """Cursor team members = licenses. Opens new licenses and closes those of people who left."""
    if not emails:
        # An empty list is almost always an API error; do not close every license because of it
        log.warning("cursor: empty member list, licenses left unchanged")
        return
    present = set()
    for email in emails:
        ident = db.get_or_create_identity(conn, "cursor", email)
        person = conn.execute("SELECT person_id FROM identities WHERE id = %s", (ident,)).fetchone()["person_id"]
        if person is None:
            continue
        present.add(person)
        open_ = conn.execute(
            "SELECT 1 FROM ai_licenses WHERE person_id = %s AND tool = 'cursor' AND valid_to IS NULL", (person,)
        ).fetchone()
        if not open_:
            conn.execute(
                "INSERT INTO ai_licenses (person_id, tool, valid_from) VALUES (%s, 'cursor', %s) ON CONFLICT DO NOTHING",
                (person, today),
            )
    conn.execute(
        "UPDATE ai_licenses SET valid_to = %s WHERE tool = 'cursor' AND valid_to IS NULL AND NOT (person_id = ANY(%s))",
        (today, list(present)),
    )
