"""Servidor HTTP: recetor OTLP do Claude Code e webhooks de deploys e incidentes."""
from __future__ import annotations

import datetime as dt
import hmac
import logging

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from . import db, otel
from .config import Settings

log = logging.getLogger("uvicorn.error")
settings = Settings()
app = FastAPI(title="Outsmart AI metrics")


def _clean(value: str | None) -> str:
    # Tolera espaços, \r de ficheiros .env editados no Windows e aspas à volta do valor
    return (value or "").strip().strip("\"'").strip()


def _check(expected: str | None, authorization: str | None) -> None:
    expected = _clean(expected)
    if not expected:
        raise HTTPException(503, "token não configurado no servidor")
    raw = _clean(authorization)
    given = _clean(raw[7:] if raw.lower().startswith("bearer ") else raw)
    if not hmac.compare_digest(given.encode(), expected.encode()):
        # Nunca registar o token; só o suficiente para perceber a diferença
        if not raw:
            log.warning("401: request without an Authorization header")
        else:
            log.warning("401: token mismatch (received %d characters starting with %r; expected %d characters starting with %r)",
                        len(given), given[:2], len(expected), expected[:2])
        raise HTTPException(401, "token inválido")


def otel_auth(authorization: str | None = Header(default=None)):
    _check(settings.otel_token, authorization)


def events_auth(authorization: str | None = Header(default=None)):
    _check(settings.events_token, authorization)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/otel/v1/metrics", dependencies=[Depends(otel_auth)])
async def otel_metrics(request: Request):
    ctype = request.headers.get("content-type", "")
    if "json" not in ctype:
        # Claude Code tem de usar OTEL_EXPORTER_OTLP_PROTOCOL=http/json
        raise HTTPException(415, "usar OTLP http/json")
    payload = await request.json()
    stats: dict = {}
    with db.connect(settings.database_url) as conn:
        buckets = otel.parse(payload, otel.make_series_delta(conn), stats)
        otel.store(conn, buckets)
        conn.commit()
    log.info("otel: %d points, %d used, %d without user.email, emails=%s, metrics=%s",
             stats["points"], stats["used"], stats["no_email"],
             sorted({e for e, _ in buckets}), sorted(stats["metrics"]))
    return {"partialSuccess": {}}


@app.post("/otel/v1/logs", dependencies=[Depends(otel_auth)])
async def otel_logs():
    # Eventos não são usados; aceitar evita erros no Claude Code se OTEL_LOGS_EXPORTER estiver ligado.
    return {"partialSuccess": {}}


class DeploymentEvent(BaseModel):
    id: str
    repo: str
    sha: str
    environment: str = "production"
    deployed_at: dt.datetime | None = None
    previous_sha: str | None = None
    merged_pr_numbers: list[int] = []


class IncidentEvent(BaseModel):
    id: str
    title: str | None = None
    opened_at: dt.datetime
    resolved_at: dt.datetime | None = None
    deployment_id: str | None = None


@app.post("/events/deployment", dependencies=[Depends(events_auth)])
def deployment(ev: DeploymentEvent):
    with db.connect(settings.database_url) as conn:
        rid = db.repo_id(conn, ev.repo)
        conn.execute(
            "INSERT INTO deployments (id, repo_id, environment, sha, deployed_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET sha = EXCLUDED.sha, deployed_at = EXCLUDED.deployed_at",
            (ev.id, rid, ev.environment, ev.sha, ev.deployed_at or dt.datetime.now(dt.timezone.utc)),
        )
        if ev.merged_pr_numbers:
            conn.execute(
                """INSERT INTO deployment_prs (deployment_id, pr_id)
                   SELECT %s, id FROM pull_requests WHERE repo_id = %s AND number = ANY(%s) ON CONFLICT DO NOTHING""",
                (ev.id, rid, ev.merged_pr_numbers),
            )
        elif ev.previous_sha:
            db.set_state(conn, f"deploy_link:{ev.id}", f"{ev.repo}|{ev.previous_sha}|{ev.sha}")
        conn.commit()
    return {"ok": True}


@app.post("/events/incident", dependencies=[Depends(events_auth)])
def incident(ev: IncidentEvent):
    with db.connect(settings.database_url) as conn:
        conn.execute(
            """INSERT INTO incidents (id, title, opened_at, resolved_at, caused_by_deployment) VALUES (%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, resolved_at=EXCLUDED.resolved_at,
               caused_by_deployment=COALESCE(EXCLUDED.caused_by_deployment, incidents.caused_by_deployment)""",
            (ev.id, ev.title, ev.opened_at, ev.resolved_at, ev.deployment_id),
        )
        conn.commit()
    return {"ok": True}
