from aimetrics.ai_tools import claude_record_to_activity, cursor_record_to_activity
from aimetrics.otel import CUMULATIVE, parse

# Exemplo da documentação da Claude Code Analytics API
CLAUDE_REC = {
    "date": "2025-09-08T00:00:00Z",
    "actor": {"type": "user_actor", "email_address": "developer@company.com"},
    "core_metrics": {"num_sessions": 5, "lines_of_code": {"added": 1543, "removed": 892},
                     "commits_by_claude_code": 12, "pull_requests_by_claude_code": 2},
    "tool_actions": {"edit_tool": {"accepted": 45, "rejected": 5}, "multi_edit_tool": {"accepted": 12, "rejected": 2},
                     "write_tool": {"accepted": 8, "rejected": 1}, "notebook_edit_tool": {"accepted": 3, "rejected": 0}},
    "model_breakdown": [{"model": "claude-opus-5-5",
                         "tokens": {"input": 100000, "output": 35000, "cache_read": 10000, "cache_creation": 5000},
                         "estimated_cost": {"currency": "USD", "amount": 113}}],
}


def test_claude_record():
    source, value, a = claude_record_to_activity(CLAUDE_REC)
    assert (source, value) == ("claude", "developer@company.com")
    assert a["day"] == "2025-09-08"
    assert a["actions_accepted"] == 68 and a["actions_rejected"] == 8
    assert a["tokens"] == 150000
    assert a["cost_usd"] == 1.13  # a API devolve cêntimos
    assert a["is_active"]


def test_claude_api_actor():
    rec = {**CLAUDE_REC, "actor": {"type": "api_actor", "api_key_name": "ci-key"}}
    assert claude_record_to_activity(rec)[:2] == ("claude_api_key", "ci-key")


def test_cursor_record():
    rec = {"date": 1710720000000, "isActive": True, "acceptedLinesAdded": 1102, "acceptedLinesDeleted": 645,
           "totalAccepts": 73, "totalRejects": 14, "totalTabsAccepted": 289, "chatRequests": 128,
           "composerRequests": 45, "agentRequests": 12, "cmdkUsages": 67, "email": "developer@company.com"}
    a = cursor_record_to_activity(rec)
    assert a["day"] == "2024-03-18"
    assert a["actions_accepted"] == 362 and a["chat_requests"] == 240 and a["is_active"]


def test_cursor_inactive_day():
    a = cursor_record_to_activity({"date": 1710720000000, "isActive": False})
    assert not a["is_active"]


def _dp(value, ns, **attrs):
    return {"asInt": str(value), "timeUnixNano": str(ns), "startTimeUnixNano": "1",
            "attributes": [{"key": k.replace("_", "."), "value": {"stringValue": v}} for k, v in attrs.items()]}


def _payload(metrics, temporality=1):
    return {"resourceMetrics": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
        "scopeMetrics": [{"metrics": [
            {"name": name, "sum": {"aggregationTemporality": temporality, "isMonotonic": True, "dataPoints": dps}}
            for name, dps in metrics]}]}]}


NS = 1_760_000_000_000_000_000  # 2025-10-09


def test_otel_delta():
    payload = _payload([
        ("claude_code.lines_of_code.count", [_dp(10, NS, user_email="Dev@X.pt", type="added"),
                                             _dp(3, NS, user_email="Dev@X.pt", type="removed")]),
        ("claude_code.code_edit_tool.decision", [_dp(2, NS, user_email="dev@x.pt", decision="accept", tool_name="Edit")]),
        ("claude_code.session.count", [_dp(1, NS, user_email="dev@x.pt")]),
        ("claude_code.lines_of_code.count", [_dp(5, NS)]),  # sem email: ignorado
    ])
    out = parse(payload, lambda *a: 0)
    b = out[("dev@x.pt", "2025-10-09")]
    assert (b["lines_added"], b["lines_removed"], b["actions_accepted"], b["sessions"]) == (10, 3, 2, 1)
    assert len(out) == 1


def test_otel_cumulative_uses_delta():
    state = {}

    def delta(key, start, value):
        prev = state.get(key)
        state[key] = value
        return value - prev if prev is not None else value

    p1 = _payload([("claude_code.commit.count", [_dp(2, NS, user_email="a@x.pt")])], CUMULATIVE)
    p2 = _payload([("claude_code.commit.count", [_dp(5, NS, user_email="a@x.pt")])], CUMULATIVE)
    assert parse(p1, delta)[("a@x.pt", "2025-10-09")]["commits"] == 2
    assert parse(p2, delta)[("a@x.pt", "2025-10-09")]["commits"] == 3
