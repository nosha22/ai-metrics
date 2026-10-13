"""Rules that attribute AI involvement to pull requests.

Reproduces Swarmia's public rules (help.swarmia.com/features/ai-tools/ai-tool-detection-and-filters):

  assisted (high)  — a commit was authored or co-authored by an AI tool
  assisted (high)  — a commit has the tool's trailer (e.g. `Made-with: Cursor`)
  assisted (high)  — the PR has the tool's label (e.g. `claude-code-assisted`)
  assisted (low)   — a commit's author used the tool in the 24 h before the commit
  cloud_agent      — the PR or its first commit was created by the agent
  review_agent     — the PR has reviews from an AI bot

Outsmart extra: if Git AI recorded AI lines in the PR's commits, that is high-confidence attribution.

This module does not touch the database: it takes data and returns attributions, so it is easy to test.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

DEFAULT_PATTERNS: dict[str, dict[str, list[str]]] = {
    "claude_code": {
        # Co-authored-by: Claude <noreply@anthropic.com>  (Claude Code's default attribution)
        "coauthor": [r"noreply@anthropic\.com", r"^claude\b"],
        "trailers": [r"^claude-session:"],
        "labels": [r"^claude-code-assisted$"],
        # Commits/PRs created by the Claude GitHub Action or by cloud sessions
        "agents": [r"^claude(\[bot\])?$", r"^claude-code(\[bot\])?$"],
        "reviewers": [r"^claude(\[bot\])?$"],
    },
    "cursor": {
        "coauthor": [r"cursoragent@cursor\.com", r"^cursor agent\b"],
        "trailers": [r"^made-with:\s*cursor$"],
        "labels": [],
        "agents": [r"^cursoragent$", r"^cursor(\[bot\])?$", r"^cursor agent$"],
        "reviewers": [r"^cursor(\[bot\])?$", r"^cursor-bugbot(\[bot\])?$"],
    },
    "copilot": {
        "coauthor": [r"copilot"],
        "trailers": [],
        "labels": [],
        "agents": [r"^copilot-swe-agent(\[bot\])?$", r"^copilot$"],
        "reviewers": [r"^copilot-pull-request-reviewer(\[bot\])?$", r"^copilot$"],
    },
}

ATTRIBUTION_WINDOW = dt.timedelta(hours=24)


@dataclass
class Commit:
    sha: str
    author_name: str | None = None
    author_email: str | None = None
    author_login: str | None = None
    committer_name: str | None = None
    committer_email: str | None = None
    committer_login: str | None = None
    co_authors: list[str] = field(default_factory=list)
    trailers: list[str] = field(default_factory=list)
    committed_at: dt.datetime | None = None
    author_person: int | None = None


@dataclass
class PullRequest:
    id: int
    author_login: str | None
    labels: list[str]
    commits: list[Commit]
    reviewers: list[str] = field(default_factory=list)
    git_ai_lines: int = 0
    git_ai_added: int = 0


@dataclass(frozen=True)
class Attribution:
    tool: str
    mode: str          # assisted | cloud_agent | review_agent
    confidence: str    # high | low
    reason: str


UsedTool = Callable[[int, str, dt.datetime, dt.datetime], bool]


class Detector:
    def __init__(self, extra: dict | None = None):
        self.patterns: dict[str, dict[str, list[re.Pattern]]] = {}
        for tool, kinds in DEFAULT_PATTERNS.items():
            merged = {k: list(v) for k, v in kinds.items()}
            for kind, values in ((extra or {}).get(tool) or {}).items():
                merged.setdefault(kind, []).extend(values or [])
            self.patterns[tool] = {k: [re.compile(p, re.I) for p in v] for k, v in merged.items()}

    # --- helpers ---------------------------------------------------------
    def _any(self, tool: str, kind: str, values: Iterable[str | None]) -> str | None:
        for value in values:
            if not value:
                continue
            for pat in self.patterns[tool].get(kind, []):
                if pat.search(value.strip()):
                    return value
        return None

    def _agent_author(self, tool: str, c: Commit) -> bool:
        return bool(self._any(tool, "agents", [c.author_login, c.author_name]))

    def _agent_committer(self, tool: str, c: Commit) -> bool:
        return bool(self._any(tool, "agents", [c.committer_login, c.committer_name]))

    # --- rules -----------------------------------------------------------
    def cloud_agent(self, pr: PullRequest) -> Attribution | None:
        """The PR or its first commit was created by an agent.

        The commit author takes priority over the committer: in Swarmia's example, a commit
        authored by Claude but committed by `cursoragent` counts as a Claude Code agent.
        """
        first = pr.commits[0] if pr.commits else None
        for tool in self.patterns:
            if self._any(tool, "agents", [pr.author_login]):
                return Attribution(tool, "cloud_agent", "high", f"PR opened by {pr.author_login}")
        if first:
            for tool in self.patterns:
                if self._agent_author(tool, first):
                    return Attribution(tool, "cloud_agent", "high", f"first commit {first.sha[:7]} authored by {first.author_login or first.author_name}")
            for tool in self.patterns:
                if self._agent_committer(tool, first):
                    return Attribution(tool, "cloud_agent", "high", f"first commit {first.sha[:7]} committed by {first.committer_login or first.committer_name}")
        return None

    def assisted_high(self, tool: str, pr: PullRequest) -> Attribution | None:
        for c in pr.commits:
            if self._agent_author(tool, c):
                return Attribution(tool, "assisted", "high", f"commit {c.sha[:7]} authored by the agent")
            hit = self._any(tool, "coauthor", c.co_authors)
            if hit:
                return Attribution(tool, "assisted", "high", f"co-author '{hit}' in {c.sha[:7]}")
            hit = self._any(tool, "trailers", c.trailers)
            if hit:
                return Attribution(tool, "assisted", "high", f"trailer '{hit}' in {c.sha[:7]}")
        hit = self._any(tool, "labels", pr.labels)
        if hit:
            return Attribution(tool, "assisted", "high", f"label '{hit}' on the PR")
        return None

    def assisted_low(self, tool: str, pr: PullRequest, used_tool: UsedTool | None) -> Attribution | None:
        if used_tool is None:
            return None
        for c in pr.commits:
            if c.author_person is None or c.committed_at is None:
                continue
            if used_tool(c.author_person, tool, c.committed_at - ATTRIBUTION_WINDOW, c.committed_at):
                return Attribution(tool, "assisted", "low", f"author used the tool in the 24 h before {c.sha[:7]}")
        return None

    def review_agents(self, pr: PullRequest) -> list[Attribution]:
        out = []
        for tool in self.patterns:
            hit = self._any(tool, "reviewers", pr.reviewers)
            if hit:
                out.append(Attribution(tool, "review_agent", "high", f"review by {hit}"))
        return out

    def attribute(self, pr: PullRequest, used_tool: UsedTool | None = None) -> list[Attribution]:
        out: dict[tuple[str, str], Attribution] = {}

        def add(a: Attribution | None):
            if a is None:
                return
            key = (a.tool, a.mode)
            # High confidence replaces low for the same tool and mode
            if key not in out or (out[key].confidence == "low" and a.confidence == "high"):
                out[key] = a

        agent = self.cloud_agent(pr)
        if agent:
            add(agent)
            # An agent PR also counts as assisted (as in Swarmia)
            add(Attribution(agent.tool, "assisted", "high", "PR created by an agent"))

        for tool in self.patterns:
            add(self.assisted_high(tool, pr))
            if (tool, "assisted") not in out:
                add(self.assisted_low(tool, pr, used_tool))

        if pr.git_ai_lines > 0:
            share = pr.git_ai_lines / pr.git_ai_added if pr.git_ai_added else 0
            add(Attribution("git_ai", "assisted", "high", f"Git AI: {pr.git_ai_lines} AI lines ({share:.0%})"))

        for a in self.review_agents(pr):
            add(a)
        return list(out.values())
