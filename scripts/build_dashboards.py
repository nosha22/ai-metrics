"""Generates the Grafana dashboards in grafana/dashboards/*.json.

Dashboards are code: edit this file and run `python scripts/build_dashboards.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
DS = {"type": "grafana-postgresql-datasource", "uid": "aimetrics-pg"}

AI_GROUP = ("CASE WHEN (${high_only} AND ai_assisted_high) OR (NOT ${high_only} AND ai_assisted) "
            "THEN 'With AI' ELSE 'Without AI' END")
TEAM = "team IN (${team:sqlstring})"


class Board:
    def __init__(self, uid: str, title: str, description: str, extra_vars: list | None = None):
        self.uid, self.title, self.description = uid, title, description
        self.panels: list[dict] = []
        self.y = 0
        self.vars = [team_var()] + (extra_vars or [])
        self._id = 0

    def _pid(self):
        self._id += 1
        return self._id

    def row(self, title: str):
        self.panels.append({"type": "row", "title": title, "id": self._pid(), "collapsed": False,
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}, "panels": []})
        self.y += 1

    def add(self, panels: list[tuple[dict, int]], h: int = 8):
        x = 0
        for panel, w in panels:
            panel["id"] = self._pid()
            panel["gridPos"] = {"h": h, "w": w, "x": x, "y": self.y}
            panel.setdefault("datasource", DS)
            self.panels.append(panel)
            x += w
        self.y += h

    def text(self, md: str, h: int = 3):
        self.add([({"type": "text", "title": "", "options": {"mode": "markdown", "content": md}}, 24)], h)

    def json(self) -> dict:
        return {
            "uid": self.uid, "title": self.title, "description": self.description, "tags": ["ai-metrics"],
            "timezone": "browser", "schemaVersion": 39, "version": 1, "editable": True,
            "time": {"from": "now-90d", "to": "now"}, "refresh": "",
            "templating": {"list": self.vars}, "panels": self.panels,
        }


def team_var() -> dict:
    return {"name": "team", "label": "Team", "type": "query", "datasource": DS, "multi": True, "includeAll": True,
            "current": {"text": "All", "value": "$__all"}, "refresh": 1, "sort": 1,
            "query": "SELECT name FROM teams UNION SELECT '(no team)' ORDER BY 1",
            "definition": "SELECT name FROM teams"}


def high_only_var() -> dict:
    return {"name": "high_only", "label": "High confidence only", "type": "custom", "query": "false,true",
            "current": {"text": "false", "value": "false"},
            "options": [{"text": "false", "value": "false", "selected": True}, {"text": "true", "value": "true", "selected": False}],
            "description": "true = ignore the 24-hour rule (fewer false positives, some undercounting)"}


def q(sql: str, fmt: str = "table", ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DS, "rawQuery": True, "editorMode": "code", "format": fmt, "rawSql": sql.strip()}


def stat(title, sql, unit="percentunit", desc="", decimals=0):
    return {"type": "stat", "title": title, "description": desc, "targets": [q(sql)],
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "color": {"mode": "fixed", "fixedColor": "blue"}}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "values": False}, "colorMode": "value",
                        "graphMode": "none", "textMode": "value"}}


def timeseries(title, sql, unit="percentunit", desc="", bars=False, minv=None, maxv=None):
    custom = {"drawStyle": "bars" if bars else "line", "lineWidth": 2, "fillOpacity": 60 if bars else 0,
              "pointSize": 5, "showPoints": "auto", "spanNulls": True}
    defaults = {"unit": unit, "custom": custom}
    if minv is not None:
        defaults["min"] = minv
    if maxv is not None:
        defaults["max"] = maxv
    return {"type": "timeseries", "title": title, "description": desc, "targets": [q(sql, "time_series")],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}}}


def table(title, sql, desc="", units: dict | None = None):
    overrides = [{"matcher": {"id": "byName", "options": k}, "properties": [{"id": "unit", "value": v}]}
                 for k, v in (units or {}).items()]
    return {"type": "table", "title": title, "description": desc, "targets": [q(sql)],
            "fieldConfig": {"defaults": {}, "overrides": overrides}, "options": {"showHeader": True}}


def barchart(title, sql, unit="h", desc=""):
    return {"type": "barchart", "title": title, "description": desc, "targets": [q(sql)],
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {"orientation": "horizontal", "showValue": "auto", "legend": {"displayMode": "list", "placement": "bottom"},
                        "xTickLabelRotation": 0, "groupWidth": 0.7, "barWidth": 0.9}}


# ---------------------------------------------------------------------------
def adoption() -> Board:
    b = Board("ai-adoption", "AI · Adoption", "Licenses, active users and idle seats (Swarmia definitions).")
    b.text("**Active** = accepted code or used chat during the week. **Licensed** = had a license at some point "
           "during the week; for sources without a license list (OpenTelemetry), only people who were active count.")
    window = "week >= date_trunc('week', $__timeFrom()::date) AND week <= $__timeTo()::date AND " + TEAM
    b.add([
        (stat("License rate", f"SELECT sum(enabled)::numeric / NULLIF(sum(members),0) FROM v_adoption_weekly "
              f"WHERE week = date_trunc('week', current_date)::date AND {TEAM}",
              desc="Members with a license ÷ members, current week, all tools added together"), 6),
        (stat("Active rate (avg)", f"SELECT avg(r) FROM (SELECT week, sum(weekly_active)::numeric / NULLIF(sum(members),0) r "
              f"FROM (SELECT week, team, max(weekly_active) weekly_active, max(members) members FROM v_adoption_weekly "
              f"WHERE {window} GROUP BY week, team) x GROUP BY week) w",
              desc="Weekly average of active members ÷ members. Someone active in two tools counts once per team (max)."), 6),
        (stat("Idle seats", f"SELECT count(*) FROM v_idle_seats WHERE {TEAM}", unit="none",
              desc="Active license with no activity in the last 28 days"), 6),
        (stat("Unlinked accounts", "SELECT count(*) FROM identities WHERE person_id IS NULL", unit="none",
              desc="Run `aimetrics org unlinked` and add them to organization.yml"), 6),
    ], h=5)
    b.add([
        (timeseries("Weekly active rate by tool",
                    f"SELECT week AS time, tool AS metric, sum(weekly_active)::numeric / NULLIF(sum(members),0) AS value "
                    f"FROM v_adoption_weekly WHERE {window} GROUP BY 1,2 ORDER BY 1", minv=0, maxv=1), 12),
        (timeseries("Active users per week", f"SELECT week AS time, tool AS metric, sum(weekly_active) AS value "
                    f"FROM v_adoption_weekly WHERE {window} GROUP BY 1,2 ORDER BY 1", unit="none", bars=True), 12),
    ], h=9)
    b.add([
        (table("Adoption by team (selected period)",
               f"SELECT team AS \"Team\", tool AS \"Tool\", max(members) AS \"Members\", "
               f"max(enabled)::numeric / NULLIF(max(members),0) AS \"License rate\", "
               f"avg(weekly_active::numeric / NULLIF(members,0)) AS \"Active rate\" "
               f"FROM v_adoption_weekly WHERE {window} GROUP BY 1,2 ORDER BY 1,2",
               units={"License rate": "percentunit", "Active rate": "percentunit"}), 12),
        (table("Idle seats", f"SELECT team AS \"Team\", name AS \"Person\", tool AS \"Tool\", "
               f"valid_from AS \"Licensed since\", last_active_day AS \"Last active day\" FROM v_idle_seats WHERE {TEAM} ORDER BY 1,2",
               desc="Talk to each person before removing a license"), 12),
    ], h=9)
    b.row("Activity")
    b.add([
        (timeseries("Action acceptance rate", f"SELECT date_trunc('week', a.day) AS time, a.tool AS metric, "
                    f"sum(a.actions_accepted)::numeric / NULLIF(sum(a.actions_accepted + a.actions_rejected),0) AS value "
                    f"FROM v_ai_activity a JOIN v_members m USING (person_id) WHERE $__timeFilter(a.day) AND m.{TEAM} "
                    f"GROUP BY 1,2 ORDER BY 1", minv=0, maxv=1,
                    desc="Accepted ÷ (accepted + rejected). Does not measure delivered code."), 8),
        (timeseries("Lines generated by AI (not necessarily committed)",
                    f"SELECT date_trunc('week', a.day) AS time, a.tool AS metric, sum(a.lines_added) AS value "
                    f"FROM v_ai_activity a JOIN v_members m USING (person_id) WHERE $__timeFilter(a.day) AND m.{TEAM} "
                    f"GROUP BY 1,2 ORDER BY 1", unit="none", bars=True,
                    desc="As Swarmia warns, someone can accept 200 lines and end up with a 10-line PR. Use the Git AI dashboard for delivered code."), 8),
        (timeseries("Estimated cost (USD)", f"SELECT date_trunc('week', a.day) AS time, a.tool AS metric, sum(a.cost_usd) AS value "
                    f"FROM v_ai_activity a JOIN v_members m USING (person_id) WHERE $__timeFilter(a.day) AND m.{TEAM} "
                    f"GROUP BY 1,2 ORDER BY 1", unit="currencyUSD", bars=True,
                    desc="Claude Code only (Analytics API and OpenTelemetry). The Cursor daily API does not report cost."), 8),
    ], h=9)
    return b


def impact() -> Board:
    b = Board("ai-impact", "AI · PR impact", "AI-assisted PRs vs the rest: throughput, cycle time, review time and size.",
              [high_only_var()])
    base = f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM}"
    b.text("A PR is **AI-assisted** if a commit has an AI co-author, trailer or author, if it has the tool's label, "
           "if Git AI recorded AI lines, or (low confidence) if the author used the tool in the 24 hours before a commit. "
           "See the reason for each PR in the table at the bottom. Median and p75 limit the effect of outliers.")
    b.add([
        (stat("PRs with AI", f"SELECT avg(CASE WHEN {AI_GROUP} = 'With AI' THEN 1 ELSE 0 END) {base}"), 6),
        (stat("Agent PRs", f"SELECT avg(cloud_agent::int) {base}", desc="PR or its first commit created by an agent"), 6),
        (stat("PRs with AI review", f"SELECT avg(review_agent::int) {base}"), 6),
        (stat("Merged PRs", f"SELECT count(*) {base}", unit="none"), 6),
    ], h=5)
    b.add([
        (timeseries("Share of PRs with AI per week", f"SELECT merged_week AS time, "
                    f"avg(CASE WHEN {AI_GROUP} = 'With AI' THEN 1 ELSE 0 END) AS \"PRs with AI\", "
                    f"avg(cloud_agent::int) AS \"Agent PRs\", avg(review_agent::int) AS \"AI review\" "
                    f"{base} GROUP BY 1 ORDER BY 1", minv=0, maxv=1), 12),
        (timeseries("Throughput (merged PRs per week)", f"SELECT merged_week AS time, {AI_GROUP} AS metric, count(*) AS value "
                    f"{base} GROUP BY 1,2 ORDER BY 1", unit="none", bars=True), 12),
    ], h=9)

    def cmp(metric, label, unit):
        return barchart(f"{label}: median and p75", f"SELECT {AI_GROUP} AS \"Group\", "
                        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {metric}) AS \"Median\", "
                        f"percentile_cont(0.75) WITHIN GROUP (ORDER BY {metric}) AS \"p75\" "
                        f"{base} AND {metric} IS NOT NULL GROUP BY 1 ORDER BY 1", unit=unit)

    b.add([(cmp("cycle_time_h", "Cycle time", "h"), 8), (cmp("review_time_h", "Time to first human review", "h"), 8),
           (cmp("batch_size", "Batch size (lines +/−)", "none"), 8)], h=8)
    b.add([
        (timeseries("Median cycle time per week", f"SELECT merged_week AS time, {AI_GROUP} AS metric, "
                    f"percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_time_h) AS value {base} GROUP BY 1,2 ORDER BY 1", unit="h"), 12),
        (table("By tool", f"SELECT COALESCE(t, 'No AI') AS \"Tool\", count(*) AS \"PRs\", "
               f"percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_time_h) AS \"Cycle time (median)\", "
               f"percentile_cont(0.5) WITHIN GROUP (ORDER BY review_time_h) AS \"Review (median)\", "
               f"percentile_cont(0.5) WITHIN GROUP (ORDER BY batch_size) AS \"Batch size (median)\" "
               f"FROM v_pr_facts LEFT JOIN LATERAL unnest(NULLIF(ai_tools, '{{}}')) t ON TRUE "
               f"WHERE $__timeFilter(merged_at) AND {TEAM} GROUP BY 1 ORDER BY 2 DESC",
               desc="A PR with two tools counts once in each (as in Swarmia).",
               units={"Cycle time (median)": "h", "Review (median)": "h"}), 12),
    ], h=9)
    b.row("Details")
    b.add([(table("PRs and attribution reason",
                  f"SELECT f.merged_at AS \"Merged\", f.repo || '#' || f.number AS \"PR\", f.title AS \"Title\", f.person AS \"Person\", "
                  f"string_agg(a.tool || ' · ' || a.mode || ' · ' || a.confidence || ': ' || a.reason, E'\\n' ORDER BY a.tool) AS \"Attribution\", "
                  f"f.cycle_time_h AS \"Cycle time\", f.batch_size AS \"Batch\" "
                  f"FROM v_pr_facts f LEFT JOIN pr_ai_attribution a ON a.pr_id = f.pr_id "
                  f"WHERE $__timeFilter(f.merged_at) AND f.{TEAM} GROUP BY f.pr_id, f.merged_at, f.repo, f.number, f.title, "
                  f"f.person, f.cycle_time_h, f.batch_size ORDER BY 1 DESC LIMIT 500", units={"Cycle time": "h"}), 24)], h=12)
    return b


def code() -> Board:
    b = Board("ai-code", "AI · Generated code & quality", "Share of AI code (Git AI), line survival and change failure rate.",
              [high_only_var()])
    base = f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM} AND added_lines > 0"
    b.text("The **share of AI code** comes from Git AI notes and only covers commits made with Git AI installed. Check the "
           "coverage next to it before drawing conclusions. **Survival** = lines from each PR that still exist on the main branch "
           "(git blame). Never look at the AI share without survival next to it.")
    b.add([
        (stat("AI code share", f"SELECT sum(ai_lines)::numeric / NULLIF(sum(added_lines),0) {base}"), 6),
        (stat("Git AI coverage", f"SELECT sum(commits_with_note)::numeric / NULLIF(sum(commit_count),0) "
              f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM}",
              desc="Commits with a Git AI note ÷ commits. Below ~80% the AI share is not representative."), 6),
        (stat("Survival at 30+ days (AI ≥50%)", f"SELECT avg(survival_rate) FROM v_survival WHERE age_days >= 30 "
              f"AND ai_group = 'Mostly AI (>=50%)' AND {TEAM}"), 6),
        (stat("Survival at 30+ days (no AI)", f"SELECT avg(survival_rate) FROM v_survival WHERE age_days >= 30 "
              f"AND ai_group = 'No AI' AND {TEAM}"), 6),
    ], h=5)
    b.add([
        (timeseries("AI code share per week", f"SELECT merged_week AS time, team AS metric, "
                    f"sum(ai_lines)::numeric / NULLIF(sum(added_lines),0) AS value {base} GROUP BY 1,2 ORDER BY 1", minv=0, maxv=1), 12),
        (table("By repository", f"SELECT repo AS \"Repo\", sum(ai_lines) AS \"AI lines\", sum(human_lines) AS \"Human lines\", "
               f"sum(unknown_lines) AS \"Unattested\", sum(ai_lines)::numeric / NULLIF(sum(added_lines),0) AS \"AI share\", "
               f"sum(commits_with_note)::numeric / NULLIF(sum(commit_count),0) AS \"Coverage\" "
               f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM} GROUP BY 1 ORDER BY 2 DESC",
               units={"AI share": "percentunit", "Coverage": "percentunit"}), 12),
    ], h=9)
    b.row("Quality")
    b.add([
        (barchart("Line survival by PR age",
                  f"SELECT CASE WHEN age_days < 14 THEN '0–13 days' WHEN age_days < 30 THEN '14–29 days' "
                  f"WHEN age_days < 60 THEN '30–59 days' WHEN age_days < 90 THEN '60–89 days' ELSE '90+ days' END AS \"Age\", "
                  f"avg(survival_rate) FILTER (WHERE ai_group = 'Mostly AI (>=50%)') AS \"Mostly AI\", "
                  f"avg(survival_rate) FILTER (WHERE ai_group = 'Partly AI (<50%)') AS \"Partly AI\", "
                  f"avg(survival_rate) FILTER (WHERE ai_group = 'No AI') AS \"No AI\" "
                  f"FROM v_survival WHERE {TEAM} GROUP BY 1 ORDER BY 1", unit="percentunit",
                  desc="Half-life = the age at which survival drops below 50%. Rework at 30 days = 1 − survival at 30–59 days."), 12),
        (timeseries("Change failure rate (production)", "SELECT week AS time, ai_group AS metric, cfr AS value FROM v_cfr_weekly "
                    "WHERE $__timeFilter(week) ORDER BY 1", minv=0, maxv=1,
                    desc="Deployments with an incident ÷ deployments. 'With AI' = the deployment includes at least one AI-assisted PR (high confidence)."), 12),
    ], h=9)
    return b


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for board in (adoption(), impact(), code()):
        (OUT / f"{board.uid}.json").write_text(json.dumps(board.json(), indent=2, ensure_ascii=False) + "\n")
        print("written", board.uid)


if __name__ == "__main__":
    main()
