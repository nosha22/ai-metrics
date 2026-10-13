"""End-to-end test against a real Postgres (TEST_DATABASE_URL) with a simulated GitHub."""
import datetime as dt
import json
import os
import stat
import subprocess
import textwrap

import httpx
import psycopg
import pytest

from aimetrics import attribution, db, github, gitrepo, org
from aimetrics.ai_tools import UPSERT_ACTIVITY, cursor_record_to_activity
from aimetrics.detection import Detector

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(d):
    return d.isoformat().replace("+00:00", "Z")


ORG = {
    "teams": [{"name": "Mobile"}],
    "repositories": [{"name": "acme/app", "team": "Mobile"}],
    "people": [
        {"name": "Ana", "team": "Mobile", "identities": {"github": "ana", "git_email": "ana@acme.pt", "claude": "ana@acme.pt"},
         "licenses": [{"tool": "claude_code", "from": "2026-01-01"}]},
        {"name": "Rui", "team": "Mobile", "identities": {"github": "rui", "git_email": ["rui@acme.pt"], "cursor": "rui@acme.pt"},
         "licenses": [{"tool": "cursor", "from": "2026-01-01"}]},
        {"name": "Eva", "team": "Mobile", "identities": {"github": "eva", "git_email": "eva@acme.pt"},
         "licenses": [{"tool": "cursor", "from": "2026-01-01"}]},
    ],
}


def commit(sha, name, email, login, when, msg, committer=None):
    return {"sha": sha, "author": {"login": login} if login else None,
            "committer": {"login": committer} if committer else None,
            "commit": {"author": {"name": name, "email": email, "date": iso(when)},
                       "committer": {"name": committer or name, "email": email, "date": iso(when)}, "message": msg}}


def make_prs():
    t = NOW - dt.timedelta(days=2)
    prs = {
        1: dict(user="ana", commits=[commit("a" * 40, "Ana", "ana@acme.pt", "ana", t,
               "Add login\n\nCo-Authored-By: Claude <noreply@anthropic.com>")], add=120, merged=t + dt.timedelta(hours=5)),
        2: dict(user="rui", commits=[commit("b" * 40, "Rui", "rui@acme.pt", "rui", t, "Fix crash")],
                add=30, merged=t + dt.timedelta(hours=30), reviews=["cursor[bot]", "ana"]),
        3: dict(user="claude[bot]", commits=[commit("c" * 40, "claude[bot]", "bot@users.noreply.github.com", "claude[bot]", t,
               "Bump deps")], add=10, merged=t + dt.timedelta(hours=2)),
        4: dict(user="eva", commits=[commit("d" * 40, "Eva", "eva@acme.pt", "eva", t, "Docs")], add=5,
                merged=t + dt.timedelta(hours=3)),
    }
    return prs, t


def transport(prs, t):
    def handler(req: httpx.Request):
        path = req.url.path
        if path == "/repos/acme/app/pulls":
            return httpx.Response(200, json=[{"number": n, "updated_at": iso(NOW)} for n in prs])
        parts = path.split("/")
        n = int(parts[5])
        p = prs[n]
        if len(parts) == 6:
            return httpx.Response(200, json={
                "id": 1000 + n, "number": n, "title": f"PR {n}", "user": {"login": p["user"]}, "labels": [],
                "draft": False, "base": {"ref": "main"}, "created_at": iso(t), "updated_at": iso(NOW),
                "merged_at": iso(p["merged"]), "closed_at": iso(p["merged"]), "merge_commit_sha": f"{n}" * 40,
                "additions": p["add"], "deletions": 1, "changed_files": 1})
        if parts[6] == "commits":
            return httpx.Response(200, json=p["commits"])
        if parts[6] == "reviews":
            return httpx.Response(200, json=[{"id": 5000 + n * 10 + i, "user": {"login": r}, "state": "COMMENTED",
                                              "submitted_at": iso(t + dt.timedelta(hours=1 + i))}
                                             for i, r in enumerate(p.get("reviews", []))])
        if parts[6] == "comments":
            return httpx.Response(200, json=[])
        return httpx.Response(404)
    return httpx.MockTransport(handler)


@pytest.fixture()
def conn():
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with db.connect(URL) as c:
        db.apply_schema(c)
        yield c


def test_end_to_end(conn, tmp_path):
    org.sync(conn, ORG)
    prs, t = make_prs()

    # Rui used Cursor on the day of the commit → low-confidence 24 h rule
    ident = db.get_or_create_identity(conn, "cursor", "rui@acme.pt")
    rec = {"date": int(dt.datetime(t.year, t.month, t.day, tzinfo=dt.timezone.utc).timestamp() * 1000),
           "isActive": True, "totalAccepts": 4, "acceptedLinesAdded": 20}
    conn.execute(UPSERT_ACTIVITY, {"identity_id": ident, **cursor_record_to_activity(rec)})
    conn.commit()

    gh = github.GitHub("x", client=httpx.Client(base_url="https://api.github.com", transport=transport(prs, t)))
    assert github.sync_repo(conn, gh, "acme/app", NOW - dt.timedelta(days=30), Detector()) == 4
    attribution.run(conn, Detector())

    rows = {r["number"]: r for r in conn.execute("SELECT * FROM v_pr_facts").fetchall()}
    assert rows[1]["ai_assisted_high"] and rows[1]["ai_tools"] == ["claude_code"]
    assert rows[2]["ai_assisted"] and not rows[2]["ai_assisted_high"] and rows[2]["review_agent"]
    assert rows[3]["cloud_agent"]
    assert not rows[4]["ai_assisted"]
    # the human review (ana, second review), not the bot's
    assert round(rows[2]["review_time_h"]) == 2
    assert round(rows[1]["cycle_time_h"]) == 5
    assert rows[1]["person"] == "Ana" and rows[1]["team"] == "Mobile"

    reasons = {r["reason"] for r in conn.execute("SELECT reason FROM pr_ai_attribution").fetchall()}
    assert any("noreply@anthropic.com" in r for r in reasons)

    # Adoption: Ana (claude) with no activity → idle seat; Rui active on Cursor; Eva idle
    week = conn.execute(
        "SELECT * FROM v_adoption_weekly WHERE tool = 'cursor' AND week = date_trunc('week', %s::date)::date", (t,)
    ).fetchone()
    assert (week["members"], week["enabled"], week["weekly_active"]) == (3, 2, 1)
    idle = {(r["name"], r["tool"]) for r in conn.execute("SELECT * FROM v_idle_seats").fetchall()}
    assert idle == {("Ana", "claude_code"), ("Eva", "cursor")}


def test_otel_endpoint(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from aimetrics import server

    monkeypatch.setattr(server.settings, "database_url", URL)
    monkeypatch.setattr(server.settings, "otel_token", "secret")
    org.sync(conn, ORG)
    ns = int(NOW.timestamp() * 1e9)
    payload = {"resourceMetrics": [{"resource": {"attributes": []}, "scopeMetrics": [{"metrics": [
        {"name": "claude_code.session.count", "sum": {"aggregationTemporality": 1, "dataPoints": [
            {"asInt": "1", "timeUnixNano": str(ns), "attributes": [{"key": "user.email", "value": {"stringValue": "ana@acme.pt"}}]}]}},
        {"name": "claude_code.cost.usage", "sum": {"aggregationTemporality": 1, "dataPoints": [
            {"asDouble": 0.25, "timeUnixNano": str(ns), "attributes": [{"key": "user.email", "value": {"stringValue": "ana@acme.pt"}}]}]}},
    ]}]}]}
    client = TestClient(server.app)
    assert client.post("/otel/v1/metrics", json=payload).status_code == 401
    for _ in range(2):  # two delta exports add up
        r = client.post("/otel/v1/metrics", json=payload, headers={"Authorization": "Bearer secret"})
        assert r.status_code == 200
    row = conn.execute("SELECT * FROM v_ai_activity WHERE tool = 'claude_code'").fetchone()
    assert row["sessions"] == 2 and float(row["cost_usd"]) == 0.5 and row["is_active"]
    assert row["person_id"] == db.person_for(conn, "claude", "ana@acme.pt")
    idle = {r["name"] for r in conn.execute("SELECT name FROM v_idle_seats WHERE tool = 'claude_code'").fetchall()}
    assert "Ana" not in idle

    r = TestClient(server.app).post("/events/deployment", json={"id": "d1", "repo": "acme/app", "sha": "x"},
                                    headers={"Authorization": "Bearer y"})
    assert r.status_code in (401, 503)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "Ana", "GIT_AUTHOR_EMAIL": "ana@acme.pt",
                        "GIT_COMMITTER_NAME": "Ana", "GIT_COMMITTER_EMAIL": "ana@acme.pt"})


def test_git_ai_and_survival(conn, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q", "-b", "main")
    (src / "app.kt").write_text("".join(f"line {i}\n" for i in range(10)))
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "PR 1")
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    _git(src, "notes", "--ref=ai", "add", "-m", "authorship", sha)
    (src / "app.kt").write_text("".join(f"line {i}\n" if i >= 4 else f"changed {i}\n" for i in range(10)))
    _git(src, "commit", "-qam", "rework")

    dst = tmp_path / "dst"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(src), str(dst)], check=True)
    _git(dst, "fetch", "-q", "origin", "+refs/notes/ai:refs/notes/ai")

    fake = tmp_path / "git-ai"
    fake.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo '{json.dumps({"human_additions": 2, "unknown_additions": 0, "ai_additions": 8, "ai_accepted": 8,
                            "git_diff_added_lines": 10, "git_diff_deleted_lines": 0,
                            "tool_model_breakdown": {"claude-code::opus": {"ai_additions": 8, "ai_accepted": 8}}})}'
        """))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    org.sync(conn, ORG)
    rid = db.repo_id(conn, "acme/app")
    conn.execute(
        """INSERT INTO pull_requests (id, repo_id, number, author_login, created_at, merged_at, merge_commit_sha,
               additions, updated_at, initiated_by)
           VALUES (1, %s, 1, 'ana', now() - interval '40 days', now() - interval '40 days', %s, 10, now(),
                   (SELECT person_id FROM identities WHERE value='ana'))""", (rid, sha))
    conn.execute("INSERT INTO commits (sha, repo_id, pr_id, position) VALUES (%s, %s, 1, 0)", (sha, rid))
    conn.commit()

    assert gitrepo.sync_git_ai(conn, dst, rid, str(fake)) == 1
    assert gitrepo.sync_survival(conn, dst, rid) == 1
    attribution.run(conn, Detector())

    f = conn.execute("SELECT * FROM v_pr_facts WHERE pr_id = 1").fetchone()
    assert float(f["ai_share"]) == 0.8 and f["ai_assisted_high"]
    s = conn.execute("SELECT * FROM v_survival").fetchone()
    assert s["surviving_lines"] == 6 and float(s["survival_rate"]) == 0.6 and s["ai_group"] == "Mostly AI (>=50%)"
