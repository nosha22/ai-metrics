"""Applies the detection rules to stored PRs and writes pr_ai_attribution."""
from __future__ import annotations

import datetime as dt
import logging

from . import db
from .detection import Commit, Detector, PullRequest

log = logging.getLogger(__name__)


def make_used_tool(conn):
    cache: dict[tuple, bool] = {}

    def used_tool(person_id: int, tool: str, since: dt.datetime, until: dt.datetime) -> bool:
        key = (person_id, tool, since, until)
        if key not in cache:
            # OTel gives the time; the daily APIs only the day, so the whole day is used (as Swarmia does).
            row = conn.execute(
                """SELECT 1 FROM v_ai_activity
                   WHERE person_id = %s AND tool = %s AND is_active
                     AND ((last_activity_at IS NOT NULL AND last_activity_at BETWEEN %s AND %s)
                          OR (last_activity_at IS NULL AND day BETWEEN %s::date AND %s::date))
                   LIMIT 1""",
                (person_id, tool, since, until, since, until),
            ).fetchone()
            cache[key] = row is not None
        return cache[key]

    return used_tool


def load_pr(conn, pr_id: int) -> PullRequest:
    pr = conn.execute("SELECT id, author_login, labels FROM pull_requests WHERE id = %s", (pr_id,)).fetchone()
    rows = conn.execute(
        """SELECT c.*, COALESCE(ie.person_id, ig.person_id) AS person_id
           FROM commits c
           LEFT JOIN identities ie ON ie.source = 'git_email' AND ie.value = lower(c.author_email)
           LEFT JOIN identities ig ON ig.source = 'github' AND ig.value = lower(c.author_login)
           WHERE c.pr_id = %s ORDER BY c.position""",
        (pr_id,),
    ).fetchall()
    commits = [
        Commit(
            sha=r["sha"], author_name=r["author_name"], author_email=r["author_email"], author_login=r["author_login"],
            committer_name=r["committer_name"], committer_email=r["committer_email"], committer_login=r["committer_login"],
            co_authors=r["co_authors"], trailers=r["trailers"], committed_at=r["committed_at"], author_person=r["person_id"],
        )
        for r in rows
    ]
    reviewers = [r["reviewer_login"] for r in conn.execute(
        "SELECT DISTINCT reviewer_login FROM pr_reviews WHERE pr_id = %s AND reviewer_login IS NOT NULL", (pr_id,)
    ).fetchall()]
    g = conn.execute("SELECT ai_lines, added_lines FROM v_git_ai_pr WHERE pr_id = %s", (pr_id,)).fetchone()
    return PullRequest(
        id=pr["id"], author_login=pr["author_login"], labels=pr["labels"], commits=commits, reviewers=reviewers,
        git_ai_lines=(g or {}).get("ai_lines") or 0, git_ai_added=(g or {}).get("added_lines") or 0,
    )


def run(conn, detector: Detector, since: dt.datetime | None = None) -> int:
    """Recalculates the attribution of PRs updated since `since` (all of them if None)."""
    if since is None:
        ids = [r["id"] for r in conn.execute("SELECT id FROM pull_requests").fetchall()]
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM pull_requests WHERE updated_at >= %s OR merged_at >= %s", (since, since)
        ).fetchall()]
    used_tool = make_used_tool(conn)
    for pr_id in ids:
        pr = load_pr(conn, pr_id)
        attributions = detector.attribute(pr, used_tool)
        conn.execute("DELETE FROM pr_ai_attribution WHERE pr_id = %s", (pr_id,))
        for a in attributions:
            conn.execute(
                "INSERT INTO pr_ai_attribution (pr_id, tool, mode, confidence, reason) VALUES (%s,%s,%s,%s,%s)",
                (pr_id, a.tool, a.mode, a.confidence, a.reason),
            )
    conn.commit()
    log.info("attribution recalculated for %d PRs", len(ids))
    return len(ids)
