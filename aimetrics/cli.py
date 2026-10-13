"""Command line: `python -m aimetrics <command>`."""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
import time

from . import ai_tools, attribution, db, github, gitrepo, org
from .config import Settings, load_org
from .detection import Detector

log = logging.getLogger("aimetrics")


def _since(days: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)


def cmd_init_db(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        db.apply_schema(conn)
    print("schema applied")


def cmd_org_sync(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        print(org.sync(conn, load_org(s.org_file)))


def cmd_org_unlinked(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        rows = org.unlinked_identities(conn)
    if not rows:
        print("all identities are linked to a person")
    for r in rows:
        print(f"{r['source']:<15} {r['value']}")


def _detector(s: Settings) -> Detector:
    try:
        return Detector(load_org(s.org_file).get("detection"))
    except FileNotFoundError:
        return Detector()


def _repos(s: Settings) -> list[str]:
    return [r["name"] for r in load_org(s.org_file)["repositories"]]


def cmd_sync_github(s: Settings, args) -> None:
    if not s.github_token:
        sys.exit("GITHUB_TOKEN is missing")
    gh = github.GitHub(s.github_token, s.github_api)
    detector = _detector(s)
    with db.connect(s.database_url) as conn:
        for name in _repos(s):
            key = f"github:{name}"
            last = db.get_state(conn, key)
            since = _since(args.days) if args.days or not last else dt.datetime.fromisoformat(last) - dt.timedelta(hours=1)
            started = dt.datetime.now(dt.timezone.utc)
            github.sync_repo(conn, gh, name, since, detector)
            db.set_state(conn, key, started.isoformat())
            conn.commit()


def cmd_sync_deployments(s: Settings, args) -> None:
    if not s.github_token:
        sys.exit("GITHUB_TOKEN is missing")
    gh = github.GitHub(s.github_token, s.github_api)
    with db.connect(s.database_url) as conn:
        for name in _repos(s):
            github.sync_deployments(conn, gh, name, args.environment)
        # deployments received by webhook with previous_sha: link them to PRs via compare
        for row in conn.execute("SELECT key, value FROM sync_state WHERE key LIKE 'deploy_link:%'").fetchall():
            repo, base, head = row["value"].split("|")
            github.link_deployment_prs(conn, gh, repo, db.repo_id(conn, repo), row["key"].split(":", 1)[1], base, head)
            conn.execute("DELETE FROM sync_state WHERE key = %s", (row["key"],))
        conn.commit()


def cmd_sync_claude(s: Settings, args) -> None:
    if not s.anthropic_admin_key:
        sys.exit("ANTHROPIC_ADMIN_KEY is missing (Claude Console accounts only; for subscriptions use OpenTelemetry)")
    with db.connect(s.database_url) as conn:
        ai_tools.sync_claude(conn, s.anthropic_admin_key, args.days)


def cmd_sync_cursor(s: Settings, args) -> None:
    if not s.cursor_admin_key:
        sys.exit("CURSOR_ADMIN_KEY is missing")
    with db.connect(s.database_url) as conn:
        ai_tools.sync_cursor(conn, s.cursor_admin_key, args.days, manage_licenses=not args.no_licenses)


def cmd_sync_git(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        for name in _repos(s):
            path = gitrepo.ensure_clone(s.repos_dir, name, s.github_token)
            rid = db.repo_id(conn, name)
            n = gitrepo.sync_git_ai(conn, path, rid, s.git_ai_bin)
            m = gitrepo.sync_survival(conn, path, rid)
            log.info("%s: git ai on %d commits, survival on %d PRs", name, n, m)


def cmd_attribute(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        attribution.run(conn, _detector(s), None if args.all else _since(args.days))


def cmd_run(s: Settings, args) -> None:
    """Runs every available step; a step without credentials is skipped, a failing step does not stop the others."""
    steps = [("org", cmd_org_sync, True), ("github", cmd_sync_github, bool(s.github_token)),
             ("claude", cmd_sync_claude, bool(s.anthropic_admin_key)), ("cursor", cmd_sync_cursor, bool(s.cursor_admin_key)),
             ("git", cmd_sync_git, bool(s.github_token)), ("deployments", cmd_sync_deployments, bool(s.github_token)),
             ("attribute", cmd_attribute, True)]
    ns = argparse.Namespace(days=args.days, all=False, environment="production", no_licenses=False)
    failed = []
    for name, fn, enabled in steps:
        if not enabled:
            log.info("step %s skipped (no credentials)", name)
            continue
        try:
            log.info("step %s", name)
            fn(s, ns)
        except Exception:  # noqa: BLE001 — log and carry on
            log.exception("step %s failed", name)
            failed.append(name)
    if failed:
        raise SystemExit(f"steps with errors: {', '.join(failed)}")


def cmd_scheduler(s: Settings, args) -> None:
    """Runs `run` every day at the given UTC hour (and once on start-up)."""
    cmd_init_db(s, args)
    while True:
        try:
            cmd_run(s, argparse.Namespace(days=s.lookback_days))
        except SystemExit as e:
            log.error("%s", e)
        now = dt.datetime.now(dt.timezone.utc)
        nxt = now.replace(hour=args.hour, minute=7, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        log.info("next run: %s", nxt.isoformat())
        time.sleep((nxt - now).total_seconds())


def cmd_serve(s: Settings, args) -> None:
    import uvicorn

    uvicorn.run("aimetrics.server:app", host=args.host, port=args.port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings()
    p = argparse.ArgumentParser(prog="aimetrics", description="Outsmart AI metrics")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create/update tables and views").set_defaults(fn=cmd_init_db)
    o = sub.add_parser("org", help="teams, people and identities").add_subparsers(dest="org_cmd", required=True)
    o.add_parser("sync", help="apply config/organization.yml").set_defaults(fn=cmd_org_sync)
    o.add_parser("unlinked", help="list accounts not linked to a person").set_defaults(fn=cmd_org_unlinked)

    sy = sub.add_parser("sync", help="import data").add_subparsers(dest="sync_cmd", required=True)
    g = sy.add_parser("github", help="PRs, commits and reviews")
    g.add_argument("--days", type=int, default=0, help="re-import the last N days (incremental by default)")
    g.set_defaults(fn=cmd_sync_github)
    c = sy.add_parser("claude", help="Claude Code Analytics API")
    c.add_argument("--days", type=int, default=s.lookback_days)
    c.set_defaults(fn=cmd_sync_claude)
    cu = sy.add_parser("cursor", help="Cursor Admin API")
    cu.add_argument("--days", type=int, default=s.lookback_days)
    cu.add_argument("--no-licenses", action="store_true", help="do not manage licenses from the Cursor team members")
    cu.set_defaults(fn=cmd_sync_cursor)
    sy.add_parser("git", help="Git AI notes and line survival").set_defaults(fn=cmd_sync_git)
    d = sy.add_parser("deployments", help="deployments from the GitHub Deployments API")
    d.add_argument("--environment", default="production")
    d.set_defaults(fn=cmd_sync_deployments)

    a = sub.add_parser("attribute", help="recalculate the AI attribution of PRs")
    a.add_argument("--days", type=int, default=s.lookback_days)
    a.add_argument("--all", action="store_true")
    a.set_defaults(fn=cmd_attribute)

    r = sub.add_parser("run", help="all steps")
    r.add_argument("--days", type=int, default=s.lookback_days)
    r.set_defaults(fn=cmd_run)
    sc = sub.add_parser("scheduler", help="run `run` daily")
    sc.add_argument("--hour", type=int, default=3, help="UTC hour")
    sc.set_defaults(fn=cmd_scheduler)
    sv = sub.add_parser("serve", help="OTLP receiver and webhooks")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    args.fn(s, args)


if __name__ == "__main__":
    main()
