"""Conversão de métricas OTLP (JSON) do Claude Code em atividade diária.

Métricas usadas (code.claude.com/docs/en/monitoring-usage):
  claude_code.session.count             -> sessions
  claude_code.lines_of_code.count       -> lines_added / lines_removed (atributo type)
  claude_code.code_edit_tool.decision   -> actions_accepted / actions_rejected (atributo decision)
  claude_code.commit.count              -> commits
  claude_code.pull_request.count        -> pull_requests
  claude_code.token.usage               -> tokens
  claude_code.cost.usage                -> cost_usd
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

DELTA, CUMULATIVE = 1, 2

FIELDS = ("sessions", "lines_added", "lines_removed", "actions_accepted", "actions_rejected",
          "commits", "pull_requests", "tokens", "cost_usd")


def _attrs(items: list[dict] | None) -> dict:
    out = {}
    for kv in items or []:
        v = kv.get("value") or {}
        for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if k in v:
                out[kv["key"]] = v[k]
                break
    return out


def _value(dp: dict) -> float:
    if "asDouble" in dp:
        return float(dp["asDouble"])
    if "asInt" in dp:
        return float(dp["asInt"])
    return 0.0


def _field_for(name: str, attrs: dict) -> str | None:
    if name == "claude_code.session.count":
        return "sessions"
    if name == "claude_code.lines_of_code.count":
        return {"added": "lines_added", "removed": "lines_removed"}.get(attrs.get("type"))
    if name == "claude_code.code_edit_tool.decision":
        return {"accept": "actions_accepted", "reject": "actions_rejected"}.get(attrs.get("decision"))
    if name == "claude_code.commit.count":
        return "commits"
    if name == "claude_code.pull_request.count":
        return "pull_requests"
    if name == "claude_code.token.usage":
        return "tokens"
    if name == "claude_code.cost.usage":
        return "cost_usd"
    return None


def parse(payload: dict, series_delta) -> dict[tuple[str, str], dict]:
    """Agrega um pedido OTLP em {(email, dia): contadores}.

    `series_delta(key, start_ns, value) -> float` converte séries cumulativas em deltas
    (guarda o último valor visto); para séries delta devolve-se o próprio valor.
    """
    out: dict[tuple[str, str], dict] = defaultdict(lambda: {f: 0.0 for f in FIELDS} | {"last_ns": 0})
    for rm in payload.get("resourceMetrics", []):
        res = _attrs((rm.get("resource") or {}).get("attributes"))
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name", "")
                body = metric.get("sum") or metric.get("gauge") or {}
                temporality = body.get("aggregationTemporality", DELTA)
                for dp in body.get("dataPoints", []):
                    attrs = res | _attrs(dp.get("attributes"))
                    field = _field_for(name, attrs)
                    email = attrs.get("user.email")
                    if not field or not email:
                        continue
                    value = _value(dp)
                    if temporality == CUMULATIVE:
                        key = name + "|" + "|".join(f"{k}={attrs[k]}" for k in sorted(attrs))
                        value = series_delta(key, int(dp.get("startTimeUnixNano", 0)), value)
                    if value <= 0:
                        continue
                    ns = int(dp.get("timeUnixNano", 0))
                    day = dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).date().isoformat()
                    bucket = out[(email.lower(), day)]
                    bucket[field] += value
                    bucket["last_ns"] = max(bucket["last_ns"], ns)
    return dict(out)


def make_series_delta(conn):
    def series_delta(key: str, start_ns: int, value: float) -> float:
        row = conn.execute("SELECT start_ns, value FROM otel_series WHERE key = %s", (key,)).fetchone()
        conn.execute(
            "INSERT INTO otel_series (key, start_ns, value) VALUES (%s,%s,%s) "
            "ON CONFLICT (key) DO UPDATE SET start_ns = EXCLUDED.start_ns, value = EXCLUDED.value",
            (key, start_ns, value),
        )
        if row is None or row["start_ns"] != start_ns or value < row["value"]:
            return value  # série nova ou reiniciada
        return value - row["value"]

    return series_delta


UPSERT_OTEL = """
INSERT INTO ai_daily_activity (identity_id, tool, day, source, is_active, sessions, lines_added, lines_removed,
    actions_accepted, actions_rejected, commits, pull_requests, tokens, cost_usd, last_activity_at)
VALUES (%(identity_id)s, 'claude_code', %(day)s, 'otel', %(is_active)s, %(sessions)s, %(lines_added)s, %(lines_removed)s,
    %(actions_accepted)s, %(actions_rejected)s, %(commits)s, %(pull_requests)s, %(tokens)s, %(cost_usd)s, %(last)s)
ON CONFLICT (identity_id, tool, day, source) DO UPDATE SET
    is_active        = ai_daily_activity.is_active OR EXCLUDED.is_active,
    sessions         = ai_daily_activity.sessions + EXCLUDED.sessions,
    lines_added      = ai_daily_activity.lines_added + EXCLUDED.lines_added,
    lines_removed    = ai_daily_activity.lines_removed + EXCLUDED.lines_removed,
    actions_accepted = ai_daily_activity.actions_accepted + EXCLUDED.actions_accepted,
    actions_rejected = ai_daily_activity.actions_rejected + EXCLUDED.actions_rejected,
    commits          = ai_daily_activity.commits + EXCLUDED.commits,
    pull_requests    = ai_daily_activity.pull_requests + EXCLUDED.pull_requests,
    tokens           = ai_daily_activity.tokens + EXCLUDED.tokens,
    cost_usd         = ai_daily_activity.cost_usd + EXCLUDED.cost_usd,
    last_activity_at = GREATEST(ai_daily_activity.last_activity_at, EXCLUDED.last_activity_at)
"""


def store(conn, buckets: dict[tuple[str, str], dict]) -> int:
    from . import db

    for (email, day), b in buckets.items():
        ident = db.get_or_create_identity(conn, "claude", email)
        conn.execute(UPSERT_OTEL, {
            "identity_id": ident, "day": day,
            "is_active": b["sessions"] > 0 or b["actions_accepted"] > 0 or b["lines_added"] > 0 or b["tokens"] > 0,
            **{f: (round(b[f], 4) if f == "cost_usd" else int(round(b[f]))) for f in FIELDS},
            "last": dt.datetime.fromtimestamp(b["last_ns"] / 1e9, dt.timezone.utc) if b["last_ns"] else None,
        })
    return len(buckets)
