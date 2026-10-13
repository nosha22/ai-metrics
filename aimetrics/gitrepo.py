"""Local clones: Git AI notes and line survival (git blame)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import shutil
import subprocess
from pathlib import Path

from . import db

log = logging.getLogger(__name__)

EMPTY_STATS = {"human_additions": 0, "unknown_additions": 0, "ai_additions": 0, "ai_accepted": 0,
               "git_diff_added_lines": 0, "git_diff_deleted_lines": 0, "tool_model_breakdown": {}}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def ensure_clone(base: Path, full_name: str, token: str | None) -> Path:
    """Clone without checkout, with the Git AI notes and the PR refs."""
    path = base / full_name.replace("/", "__")
    url = f"https://x-access-token:{token}@github.com/{full_name}.git" if token else f"https://github.com/{full_name}.git"
    if not (path / ".git").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--no-checkout", "--quiet", url, str(path)], check=True, capture_output=True)
    else:
        git(path, "remote", "set-url", "origin", url)
    git(path, "fetch", "--quiet", "--prune", "origin",
        "+refs/heads/*:refs/remotes/origin/*",
        "+refs/pull/*/head:refs/remotes/pull/*",
        "+refs/notes/ai:refs/notes/ai", check=False)
    return path


def has_note(repo: Path, sha: str) -> bool:
    return git(repo, "notes", "--ref=ai", "show", sha, check=False).returncode == 0


def commit_stats(repo: Path, sha: str, git_ai_bin: str) -> dict | None:
    """`git ai stats <sha> --json`. Returns None if the commit has no note."""
    if not has_note(repo, sha):
        return None
    if not shutil.which(git_ai_bin):
        raise RuntimeError(f"{git_ai_bin} is not installed (needed to read Git AI notes)")
    r = subprocess.run([git_ai_bin, "stats", sha, "--json"], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        log.warning("git ai stats failed on %s: %s", sha[:7], r.stderr.strip()[:200])
        return None
    data = json.loads(r.stdout)
    return {**EMPTY_STATS, **{k: data.get(k, v) for k, v in EMPTY_STATS.items()}}


def sync_git_ai(conn, repo_path: Path, rid: int, git_ai_bin: str, refresh_days: int = 3) -> int:
    """Collects stats for PR commits that have no data yet (and recent ones, whose notes may arrive later)."""
    rows = conn.execute(
        """SELECT DISTINCT c.sha FROM commits c
           LEFT JOIN git_ai_commit_stats s ON s.repo_id = c.repo_id AND s.sha = c.sha
           WHERE c.repo_id = %s AND (s.sha IS NULL OR (NOT s.has_note AND s.collected_at > now() - make_interval(days => %s)))""",
        (rid, refresh_days),
    ).fetchall()
    n = 0
    for r in rows:
        sha = r["sha"]
        stats = commit_stats(repo_path, sha, git_ai_bin)
        s = stats or EMPTY_STATS
        conn.execute(
            """INSERT INTO git_ai_commit_stats (repo_id, sha, has_note, human_additions, unknown_additions, ai_additions,
                   ai_accepted, git_diff_added_lines, git_diff_deleted_lines, tool_model_breakdown)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (repo_id, sha) DO UPDATE SET has_note=EXCLUDED.has_note, human_additions=EXCLUDED.human_additions,
                   unknown_additions=EXCLUDED.unknown_additions, ai_additions=EXCLUDED.ai_additions,
                   ai_accepted=EXCLUDED.ai_accepted, git_diff_added_lines=EXCLUDED.git_diff_added_lines,
                   git_diff_deleted_lines=EXCLUDED.git_diff_deleted_lines,
                   tool_model_breakdown=EXCLUDED.tool_model_breakdown, collected_at=now()""",
            (rid, sha, stats is not None, s["human_additions"], s["unknown_additions"], s["ai_additions"],
             s["ai_accepted"], s["git_diff_added_lines"], s["git_diff_deleted_lines"],
             json.dumps(s["tool_model_breakdown"])),
        )
        n += 1
    conn.commit()
    return n


def default_branch(repo: Path) -> str:
    r = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    return r.stdout.strip() or "origin/main"


def surviving_lines(repo: Path, ref: str, files: list[str], origin_shas: set[str]) -> int:
    """Counts the lines in `ref` whose origin (git blame) is one of the PR's commits."""
    total = 0
    for f in files:
        r = git(repo, "blame", "--line-porcelain", "-w", ref, "--", f, check=False)
        if r.returncode != 0:
            continue  # file deleted or renamed
        for line in r.stdout.splitlines():
            if len(line) >= 40 and line[:40] in origin_shas and (len(line) == 40 or line[40] == " "):
                # Header line "<sha> <orig> <final> [n]": one per line of code
                total += 1
    return total


def changed_files(repo: Path, sha: str) -> list[str]:
    r = git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "-m", "--first-parent", sha, check=False)
    return sorted({l for l in r.stdout.splitlines() if l})


def sync_survival(conn, repo_path: Path, rid: int, max_age_days: int = 180) -> int:
    """Measures, at most once a week, how many lines of each PR still exist on the main branch."""
    ref = default_branch(repo_path)
    today = dt.date.today()
    prs = conn.execute(
        """SELECT pr.id, pr.merge_commit_sha FROM pull_requests pr
           WHERE pr.repo_id = %s AND pr.merged_at IS NOT NULL AND pr.merge_commit_sha IS NOT NULL
             AND pr.merged_at > now() - make_interval(days => %s) AND pr.additions > 0
             AND NOT EXISTS (SELECT 1 FROM line_survival s WHERE s.pr_id = pr.id AND s.measured_at > %s)""",
        (rid, max_age_days, today - dt.timedelta(days=7)),
    ).fetchall()
    n = 0
    for pr in prs:
        merge = pr["merge_commit_sha"]
        if git(repo_path, "cat-file", "-e", merge, check=False).returncode != 0:
            continue
        shas = {r["sha"] for r in conn.execute("SELECT sha FROM commits WHERE pr_id = %s", (pr["id"],)).fetchall()}
        shas.add(merge)
        files = changed_files(repo_path, merge)
        lines = surviving_lines(repo_path, ref, files, shas)
        conn.execute(
            "INSERT INTO line_survival (pr_id, measured_at, surviving_lines) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
            (pr["id"], today, lines),
        )
        n += 1
    conn.commit()
    return n
