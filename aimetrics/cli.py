"""Linha de comandos: `python -m aimetrics <comando>`."""
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
    print("schema aplicado")


def cmd_org_sync(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        print(org.sync(conn, load_org(s.org_file)))


def cmd_org_unlinked(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        rows = org.unlinked_identities(conn)
    if not rows:
        print("todas as identidades estão ligadas a uma pessoa")
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
        sys.exit("GITHUB_TOKEN em falta")
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
        sys.exit("GITHUB_TOKEN em falta")
    gh = github.GitHub(s.github_token, s.github_api)
    with db.connect(s.database_url) as conn:
        for name in _repos(s):
            github.sync_deployments(conn, gh, name, args.environment)
        # deploys recebidos por webhook com previous_sha: ligar aos PRs via compare
        for row in conn.execute("SELECT key, value FROM sync_state WHERE key LIKE 'deploy_link:%'").fetchall():
            repo, base, head = row["value"].split("|")
            github.link_deployment_prs(conn, gh, repo, db.repo_id(conn, repo), row["key"].split(":", 1)[1], base, head)
            conn.execute("DELETE FROM sync_state WHERE key = %s", (row["key"],))
        conn.commit()


def cmd_sync_claude(s: Settings, args) -> None:
    if not s.anthropic_admin_key:
        sys.exit("ANTHROPIC_ADMIN_KEY em falta (só contas Claude Console; para subscrições usar OpenTelemetry)")
    with db.connect(s.database_url) as conn:
        ai_tools.sync_claude(conn, s.anthropic_admin_key, args.days)


def cmd_sync_cursor(s: Settings, args) -> None:
    if not s.cursor_admin_key:
        sys.exit("CURSOR_ADMIN_KEY em falta")
    with db.connect(s.database_url) as conn:
        ai_tools.sync_cursor(conn, s.cursor_admin_key, args.days, manage_licenses=not args.no_licenses)


def cmd_sync_git(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        for name in _repos(s):
            path = gitrepo.ensure_clone(s.repos_dir, name, s.github_token)
            rid = db.repo_id(conn, name)
            n = gitrepo.sync_git_ai(conn, path, rid, s.git_ai_bin)
            m = gitrepo.sync_survival(conn, path, rid)
            log.info("%s: git ai em %d commits, sobrevivência em %d PRs", name, n, m)


def cmd_attribute(s: Settings, args) -> None:
    with db.connect(s.database_url) as conn:
        attribution.run(conn, _detector(s), None if args.all else _since(args.days))


def cmd_run(s: Settings, args) -> None:
    """Corre todos os passos disponíveis; um passo sem credenciais é saltado, um passo que falha não para os outros."""
    steps = [("org", cmd_org_sync, True), ("github", cmd_sync_github, bool(s.github_token)),
             ("claude", cmd_sync_claude, bool(s.anthropic_admin_key)), ("cursor", cmd_sync_cursor, bool(s.cursor_admin_key)),
             ("git", cmd_sync_git, bool(s.github_token)), ("deployments", cmd_sync_deployments, bool(s.github_token)),
             ("attribute", cmd_attribute, True)]
    ns = argparse.Namespace(days=args.days, all=False, environment="production", no_licenses=False)
    failed = []
    for name, fn, enabled in steps:
        if not enabled:
            log.info("passo %s saltado (sem credenciais)", name)
            continue
        try:
            log.info("passo %s", name)
            fn(s, ns)
        except Exception:  # noqa: BLE001 — registar e continuar
            log.exception("passo %s falhou", name)
            failed.append(name)
    if failed:
        raise SystemExit(f"passos com erro: {', '.join(failed)}")


def cmd_scheduler(s: Settings, args) -> None:
    """Corre `run` todos os dias à hora UTC indicada (e uma vez ao arrancar)."""
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
        log.info("próxima execução: %s", nxt.isoformat())
        time.sleep((nxt - now).total_seconds())


def cmd_serve(s: Settings, args) -> None:
    import uvicorn

    uvicorn.run("aimetrics.server:app", host=args.host, port=args.port, log_level="info")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    s = Settings()
    p = argparse.ArgumentParser(prog="aimetrics", description="Métricas de IA da Outsmart")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="cria/atualiza tabelas e views").set_defaults(fn=cmd_init_db)
    o = sub.add_parser("org", help="equipas, pessoas e identidades").add_subparsers(dest="org_cmd", required=True)
    o.add_parser("sync", help="aplica config/organization.yml").set_defaults(fn=cmd_org_sync)
    o.add_parser("unlinked", help="lista contas sem pessoa associada").set_defaults(fn=cmd_org_unlinked)

    sy = sub.add_parser("sync", help="importa dados").add_subparsers(dest="sync_cmd", required=True)
    g = sy.add_parser("github", help="PRs, commits e reviews")
    g.add_argument("--days", type=int, default=0, help="reimportar os últimos N dias (por omissão, incremental)")
    g.set_defaults(fn=cmd_sync_github)
    c = sy.add_parser("claude", help="Claude Code Analytics API")
    c.add_argument("--days", type=int, default=s.lookback_days)
    c.set_defaults(fn=cmd_sync_claude)
    cu = sy.add_parser("cursor", help="Cursor Admin API")
    cu.add_argument("--days", type=int, default=s.lookback_days)
    cu.add_argument("--no-licenses", action="store_true", help="não gerir licenças a partir dos membros Cursor")
    cu.set_defaults(fn=cmd_sync_cursor)
    sy.add_parser("git", help="notas do Git AI e sobrevivência de linhas").set_defaults(fn=cmd_sync_git)
    d = sy.add_parser("deployments", help="deploys da API de Deployments do GitHub")
    d.add_argument("--environment", default="production")
    d.set_defaults(fn=cmd_sync_deployments)

    a = sub.add_parser("attribute", help="recalcula a atribuição de IA dos PRs")
    a.add_argument("--days", type=int, default=s.lookback_days)
    a.add_argument("--all", action="store_true")
    a.set_defaults(fn=cmd_attribute)

    r = sub.add_parser("run", help="todos os passos")
    r.add_argument("--days", type=int, default=s.lookback_days)
    r.set_defaults(fn=cmd_run)
    sc = sub.add_parser("scheduler", help="corre `run` diariamente")
    sc.add_argument("--hour", type=int, default=3, help="hora UTC")
    sc.set_defaults(fn=cmd_scheduler)
    sv = sub.add_parser("serve", help="recetor OTLP e webhooks")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, default=8080)
    sv.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    args.fn(s, args)


if __name__ == "__main__":
    main()
