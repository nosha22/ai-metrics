import datetime as dt

from aimetrics.detection import Commit, Detector, PullRequest
from aimetrics.gitmsg import co_authors, parse_trailers

T = dt.datetime(2026, 10, 1, 12, tzinfo=dt.timezone.utc)


def pr(commits, author="sergio", labels=(), reviewers=(), **kw):
    return PullRequest(id=1, author_login=author, labels=list(labels), commits=commits, reviewers=list(reviewers), **kw)


def modes(attrs):
    return {(a.tool, a.mode, a.confidence) for a in attrs}


def test_trailers_and_coauthors_from_claude_code_message():
    msg = ("Fix login\n\nBody text.\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
           "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
           "Claude-Session: https://claude.ai/code/session_x")
    t = parse_trailers(msg)
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in t
    assert "Claude-Session: https://claude.ai/code/session_x" in t
    assert co_authors(t) == ["Claude Opus 5.5 <noreply@anthropic.com>"]


def test_no_trailers_in_single_paragraph():
    assert parse_trailers("Fix: something broke") == []


def test_claude_coauthor_is_high_confidence():
    c = Commit("a" * 40, co_authors=["Claude <noreply@anthropic.com>"])
    assert modes(Detector().attribute(pr([c]))) == {("claude_code", "assisted", "high")}


def test_cursor_trailer():
    c = Commit("b" * 40, trailers=["Made-with: Cursor"])
    assert modes(Detector().attribute(pr([c]))) == {("cursor", "assisted", "high")}


def test_label():
    c = Commit("c" * 40)
    assert modes(Detector().attribute(pr([c], labels=["claude-code-assisted"]))) == {("claude_code", "assisted", "high")}


def test_cloud_agent_pr_author():
    c = Commit("d" * 40, author_name="sergio")
    got = modes(Detector().attribute(pr([c], author="claude[bot]")))
    assert ("claude_code", "cloud_agent", "high") in got
    assert ("claude_code", "assisted", "high") in got


def test_swarmia_example_author_beats_committer():
    # 1.º commit escrito por Claude e commitado por cursoragent → agente Claude Code
    c = Commit("e" * 40, author_name="Claude", committer_name="cursoragent")
    got = modes(Detector().attribute(pr([c])))
    assert ("claude_code", "cloud_agent", "high") in got
    assert not any(t == "cursor" and m == "cloud_agent" for t, m, _ in got)


def test_cursor_agent_committer():
    c = Commit("f" * 40, author_name="Sérgio", committer_name="cursoragent")
    assert ("cursor", "cloud_agent", "high") in modes(Detector().attribute(pr([c])))


def test_low_confidence_24h_window():
    c = Commit("1" * 40, committed_at=T, author_person=7)
    calls = []

    def used(person, tool, since, until):
        calls.append((person, tool, since, until))
        return tool == "cursor"

    got = modes(Detector().attribute(pr([c]), used))
    assert got == {("cursor", "assisted", "low")}
    assert (7, "cursor", T - dt.timedelta(hours=24), T) in calls


def test_high_wins_over_low_and_skips_lookup():
    c = Commit("2" * 40, co_authors=["Claude <noreply@anthropic.com>"], committed_at=T, author_person=7)
    seen = []
    got = Detector().attribute(pr([c]), lambda p, t, s, u: seen.append(t) or True)
    assert ("claude_code", "assisted", "high") in modes(got)
    assert "claude_code" not in seen


def test_review_agent_and_git_ai():
    c = Commit("3" * 40)
    got = modes(Detector().attribute(pr([c], reviewers=["cursor[bot]", "joana"], git_ai_lines=40, git_ai_added=100)))
    assert ("cursor", "review_agent", "high") in got
    assert ("git_ai", "assisted", "high") in got


def test_human_pr_has_no_attribution():
    c = Commit("4" * 40, author_name="Sérgio Cruz", author_email="s@x.pt", committed_at=T, author_person=1)
    assert Detector().attribute(pr([c]), lambda *a: False) == []


def test_extra_patterns_from_config():
    c = Commit("5" * 40, trailers=["Assisted-by: our-internal-bot"])
    d = Detector({"claude_code": {"trailers": [r"^assisted-by: our-internal-bot$"]}})
    assert ("claude_code", "assisted", "high") in modes(d.attribute(pr([c])))
