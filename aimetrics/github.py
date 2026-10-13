"""Ingestão do GitHub: PRs, commits, reviews e deploys."""
from __future__ import annotations

import datetime as dt
import logging
import time

import httpx

from . import db
from .detection import Detector
from .gitmsg import co_authors, parse_trailers

log = logging.getLogger(__name__)


def _ts(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GitHub:
    def __init__(self, token: str, api: str = "https://api.github.com", client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=api,
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "outsmart-ai-metrics",
            },
        )

    def get(self, path: str, **params):
        for attempt in range(5):
            r = self.client.get(path, params=params)
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                reset = int(r.headers.get("x-ratelimit-reset", time.time() + 60))
                wait = max(1, reset - int(time.time())) + 1
                log.warning("rate limit do GitHub, a aguardar %ss", wait)
                time.sleep(min(wait, 900))
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    def paginate(self, path: str, **params):
        params.setdefault("per_page", 100)
        url, first = path, True
        while url:
            r = self.get(url, **(params if first else {}))
            first = False
            yield from r.json()
            url = r.links.get("next", {}).get("url")


def sync_repo(conn, gh: GitHub, full_name: str, since: dt.datetime, detector: Detector) -> int:
    """Importa os PRs atualizados desde `since`. Devolve o número de PRs importados."""
    rid = db.repo_id(conn, full_name)
    count = 0
    for item in gh.paginate(f"/repos/{full_name}/pulls", state="all", sort="updated", direction="desc"):
        if _ts(item["updated_at"]) < since:
            break
        pr = gh.get(f"/repos/{full_name}/pulls/{item['number']}").json()
        _store_pr(conn, gh, full_name, rid, pr, detector)
        count += 1
        if count % 25 == 0:
            conn.commit()
    conn.commit()
    log.info("%s: %d PRs importados", full_name, count)
    return count


def _store_pr(conn, gh: GitHub, full_name: str, rid: int, pr: dict, detector: Detector) -> None:
    number = pr["number"]
    author_login = (pr.get("user") or {}).get("login")
    author_ident = db.get_or_create_identity(conn, "github", author_login)

    commits = list(gh.paginate(f"/repos/{full_name}/pulls/{number}/commits"))
    reviews = list(gh.paginate(f"/repos/{full_name}/pulls/{number}/reviews"))
    review_comments = list(gh.paginate(f"/repos/{full_name}/pulls/{number}/comments"))

    first_commit_at = min((_ts(c["commit"]["author"]["date"]) for c in commits if c["commit"].get("author")), default=None)

    # Tempo até à primeira review humana: ignora o autor e os bots de review de IA
    def is_ai_or_bot(login: str | None) -> bool:
        return not login or login.endswith("[bot]") or bool(
            any(detector._any(t, "reviewers", [login]) for t in detector.patterns)
        )

    human_reviews = [
        _ts(r["submitted_at"]) for r in reviews
        if r.get("submitted_at") and (r.get("user") or {}).get("login") != author_login
        and not is_ai_or_bot((r.get("user") or {}).get("login"))
    ]

    initiated_by = db.person_for(conn, "github", author_login)
    conn.execute(
        """INSERT INTO pull_requests (id, repo_id, number, title, author_login, author_identity, initiated_by,
               base_branch, labels, is_draft, created_at, first_commit_at, first_review_at, merged_at, closed_at,
               merge_commit_sha, additions, deletions, changed_files, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, labels=EXCLUDED.labels, is_draft=EXCLUDED.is_draft,
               first_commit_at=EXCLUDED.first_commit_at, first_review_at=EXCLUDED.first_review_at,
               merged_at=EXCLUDED.merged_at, closed_at=EXCLUDED.closed_at, merge_commit_sha=EXCLUDED.merge_commit_sha,
               additions=EXCLUDED.additions, deletions=EXCLUDED.deletions, changed_files=EXCLUDED.changed_files,
               updated_at=EXCLUDED.updated_at, base_branch=EXCLUDED.base_branch,
               initiated_by=COALESCE(pull_requests.initiated_by, EXCLUDED.initiated_by)""",
        (pr["id"], rid, number, pr.get("title"), author_login, author_ident, initiated_by,
         (pr.get("base") or {}).get("ref"), [l["name"] for l in pr.get("labels", [])], pr.get("draft", False),
         _ts(pr["created_at"]), first_commit_at, min(human_reviews, default=None), _ts(pr.get("merged_at")),
         _ts(pr.get("closed_at")), pr.get("merge_commit_sha"), pr.get("additions", 0), pr.get("deletions", 0),
         pr.get("changed_files", 0), _ts(pr["updated_at"])),
    )

    conn.execute("DELETE FROM commits WHERE pr_id = %s", (pr["id"],))
    for pos, c in enumerate(commits):
        info = c["commit"]
        trailers = parse_trailers(info.get("message", ""))
        author_email = (info.get("author") or {}).get("email")
        author_login_c = (c.get("author") or {}).get("login")
        db.get_or_create_identity(conn, "git_email", author_email)
        if author_login_c:
            db.get_or_create_identity(conn, "github", author_login_c)
        conn.execute(
            """INSERT INTO commits (sha, repo_id, pr_id, position, author_name, author_email, author_login,
                   committer_name, committer_email, committer_login, co_authors, trailers, message_head, committed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            (c["sha"], rid, pr["id"], pos, (info.get("author") or {}).get("name"), author_email, author_login_c,
             (info.get("committer") or {}).get("name"), (info.get("committer") or {}).get("email"),
             (c.get("committer") or {}).get("login"), co_authors(trailers), trailers,
             (info.get("message") or "").splitlines()[0][:300] if info.get("message") else None,
             _ts((info.get("committer") or {}).get("date"))),
        )

    # Se o autor do PR é um agente (ex.: claude[bot]), a pessoa que o iniciou é o autor
    # humano do primeiro commit ou, na falta dele, quem o pediu fica por ligar.
    if initiated_by is None and commits:
        first = commits[0]
        email = (first["commit"].get("author") or {}).get("email")
        person = db.person_for(conn, "git_email", email) or db.person_for(conn, "github", (first.get("author") or {}).get("login"))
        if person:
            conn.execute("UPDATE pull_requests SET initiated_by = %s WHERE id = %s", (person, pr["id"]))

    comments_per_review: dict[int, int] = {}
    for rc in review_comments:
        rid_ = rc.get("pull_request_review_id")
        if rid_:
            comments_per_review[rid_] = comments_per_review.get(rid_, 0) + 1
    for r in reviews:
        conn.execute(
            """INSERT INTO pr_reviews (id, pr_id, reviewer_login, state, submitted_at, comments)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, comments=EXCLUDED.comments""",
            (r["id"], pr["id"], (r.get("user") or {}).get("login"), r.get("state"), _ts(r.get("submitted_at")),
             comments_per_review.get(r["id"], 0)),
        )


def sync_deployments(conn, gh: GitHub, full_name: str, environment: str = "production") -> int:
    """Importa deploys bem-sucedidos da API de Deployments do GitHub e liga-os aos PRs incluídos."""
    rid = db.repo_id(conn, full_name)
    deps = []
    for d in gh.paginate(f"/repos/{full_name}/deployments", environment=environment):
        statuses = gh.get(f"/repos/{full_name}/deployments/{d['id']}/statuses", per_page=10).json()
        ok = next((s for s in statuses if s.get("state") == "success"), None)
        if ok:
            deps.append((str(d["id"]), d["sha"], _ts(ok["created_at"])))
    deps.sort(key=lambda x: x[2])
    previous_sha = None
    for dep_id, sha, at in deps:
        conn.execute(
            "INSERT INTO deployments (id, repo_id, environment, sha, deployed_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING",
            (f"gh-{dep_id}", rid, environment, sha, at),
        )
        if previous_sha and previous_sha != sha:
            link_deployment_prs(conn, gh, full_name, rid, f"gh-{dep_id}", previous_sha, sha)
        previous_sha = sha
    conn.commit()
    return len(deps)


def link_deployment_prs(conn, gh: GitHub, full_name: str, rid: int, dep_id: str, base: str, head: str) -> None:
    cmp = gh.get(f"/repos/{full_name}/compare/{base}...{head}").json()
    shas = [c["sha"] for c in cmp.get("commits", [])]
    if not shas:
        return
    conn.execute(
        """INSERT INTO deployment_prs (deployment_id, pr_id)
           SELECT %s, id FROM pull_requests WHERE repo_id = %s AND merge_commit_sha = ANY(%s)
           ON CONFLICT DO NOTHING""",
        (dep_id, rid, shas),
    )
