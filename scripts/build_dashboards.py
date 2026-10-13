"""Gera os dashboards Grafana em grafana/dashboards/*.json.

Os dashboards são código: editar este ficheiro e correr `python scripts/build_dashboards.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "grafana" / "dashboards"
DS = {"type": "grafana-postgresql-datasource", "uid": "aimetrics-pg"}

AI_GROUP = ("CASE WHEN (${high_only} AND ai_assisted_high) OR (NOT ${high_only} AND ai_assisted) "
            "THEN 'Com IA' ELSE 'Sem IA' END")
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
    return {"name": "team", "label": "Equipa", "type": "query", "datasource": DS, "multi": True, "includeAll": True,
            "current": {"text": "All", "value": "$__all"}, "refresh": 1, "sort": 1,
            "query": "SELECT name FROM teams UNION SELECT '(sem equipa)' ORDER BY 1",
            "definition": "SELECT name FROM teams"}


def high_only_var() -> dict:
    return {"name": "high_only", "label": "Só alta confiança", "type": "custom", "query": "false,true",
            "current": {"text": "false", "value": "false"},
            "options": [{"text": "false", "value": "false", "selected": True}, {"text": "true", "value": "true", "selected": False}],
            "description": "true = ignora a regra das 24 h (menos falsos positivos, alguma sub-contagem)"}


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
    b = Board("ai-adoption", "IA · Adoção", "Licenças, utilizadores ativos e licenças paradas (definições do Swarmia).")
    b.text("**Ativo** = aceitou código ou usou chat na semana. **Com licença** = teve licença em algum momento "
           "da semana; para fontes sem lista de licenças (OpenTelemetry) conta só quem esteve ativo.")
    window = "week >= date_trunc('week', $__timeFrom()::date) AND week <= $__timeTo()::date AND " + TEAM
    b.add([
        (stat("Taxa de licenças", f"SELECT sum(enabled)::numeric / NULLIF(sum(members),0) FROM v_adoption_weekly "
              f"WHERE week = date_trunc('week', current_date)::date AND {TEAM}",
              desc="Membros com licença ÷ membros, semana atual, todas as ferramentas somadas"), 6),
        (stat("Taxa de atividade (média)", f"SELECT avg(r) FROM (SELECT week, sum(weekly_active)::numeric / NULLIF(sum(members),0) r "
              f"FROM (SELECT week, team, max(weekly_active) weekly_active, max(members) members FROM v_adoption_weekly "
              f"WHERE {window} GROUP BY week, team) x GROUP BY week) w",
              desc="Média semanal de ativos ÷ membros. Uma pessoa ativa em duas ferramentas conta uma vez por equipa (máximo)."), 6),
        (stat("Licenças paradas", f"SELECT count(*) FROM v_idle_seats WHERE {TEAM}", unit="none",
              desc="Licença ativa sem atividade nos últimos 28 dias"), 6),
        (stat("Contas por ligar", "SELECT count(*) FROM identities WHERE person_id IS NULL", unit="none",
              desc="Correr `aimetrics org unlinked` e acrescentar ao organization.yml"), 6),
    ], h=5)
    b.add([
        (timeseries("Taxa de atividade semanal por ferramenta",
                    f"SELECT week AS time, tool AS metric, sum(weekly_active)::numeric / NULLIF(sum(members),0) AS value "
                    f"FROM v_adoption_weekly WHERE {window} GROUP BY 1,2 ORDER BY 1", minv=0, maxv=1), 12),
        (timeseries("Ativos por semana", f"SELECT week AS time, tool AS metric, sum(weekly_active) AS value "
                    f"FROM v_adoption_weekly WHERE {window} GROUP BY 1,2 ORDER BY 1", unit="none", bars=True), 12),
    ], h=9)
    b.add([
        (table("Adoção por equipa (período)",
               f"SELECT team AS \"Equipa\", tool AS \"Ferramenta\", max(members) AS \"Membros\", "
               f"max(enabled)::numeric / NULLIF(max(members),0) AS \"Taxa de licenças\", "
               f"avg(weekly_active::numeric / NULLIF(members,0)) AS \"Taxa de atividade\" "
               f"FROM v_adoption_weekly WHERE {window} GROUP BY 1,2 ORDER BY 1,2",
               units={"Taxa de licenças": "percentunit", "Taxa de atividade": "percentunit"}), 12),
        (table("Licenças paradas", f"SELECT team AS \"Equipa\", name AS \"Pessoa\", tool AS \"Ferramenta\", "
               f"valid_from AS \"Licença desde\", last_active_day AS \"Último dia ativo\" FROM v_idle_seats WHERE {TEAM} ORDER BY 1,2",
               desc="Rever com cada pessoa antes de retirar a licença"), 12),
    ], h=9)
    b.row("Atividade")
    b.add([
        (timeseries("Taxa de aceitação de ações", f"SELECT date_trunc('week', a.day) AS time, a.tool AS metric, "
                    f"sum(a.actions_accepted)::numeric / NULLIF(sum(a.actions_accepted + a.actions_rejected),0) AS value "
                    f"FROM v_ai_activity a JOIN v_members m USING (person_id) WHERE $__timeFilter(a.day) AND m.{TEAM} "
                    f"GROUP BY 1,2 ORDER BY 1", minv=0, maxv=1,
                    desc="Aceites ÷ (aceites + rejeitadas). Não mede código entregue."), 8),
        (timeseries("Linhas geradas pela IA (não necessariamente commitadas)",
                    f"SELECT date_trunc('week', a.day) AS time, a.tool AS metric, sum(a.lines_added) AS value "
                    f"FROM v_ai_activity a JOIN v_members m USING (person_id) WHERE $__timeFilter(a.day) AND m.{TEAM} "
                    f"GROUP BY 1,2 ORDER BY 1", unit="none", bars=True,
                    desc="O Swarmia avisa: alguém pode aceitar 200 linhas para acabar num PR de 10. Usar o dashboard Git AI para código entregue."), 8),
        (timeseries("Custo estimado (USD)", f"SELECT date_trunc('week', a.day) AS time, a.tool AS metric, sum(a.cost_usd) AS value "
                    f"FROM v_ai_activity a JOIN v_members m USING (person_id) WHERE $__timeFilter(a.day) AND m.{TEAM} "
                    f"GROUP BY 1,2 ORDER BY 1", unit="currencyUSD", bars=True,
                    desc="Só Claude Code (Analytics API e OpenTelemetry). A Cursor API diária não dá custo."), 8),
    ], h=9)
    return b


def impact() -> Board:
    b = Board("ai-impact", "IA · Impacto nos PRs", "PRs assistidos por IA vs restantes: throughput, cycle time, review e tamanho.",
              [high_only_var()])
    base = f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM}"
    b.text("Um PR é **assistido** se um commit tem co-autor, trailer ou autor de IA, se tem a label da ferramenta, "
           "se o Git AI registou linhas IA, ou (baixa confiança) se o autor usou a ferramenta nas 24 h antes de um commit. "
           "Ver o motivo de cada PR na tabela no fim. Mediana e p75 limitam o efeito de outliers.")
    b.add([
        (stat("PRs com IA", f"SELECT avg(CASE WHEN {AI_GROUP} = 'Com IA' THEN 1 ELSE 0 END) {base}"), 6),
        (stat("PRs de agentes", f"SELECT avg(cloud_agent::int) {base}", desc="PR ou 1.º commit criado por um agente"), 6),
        (stat("PRs com review de IA", f"SELECT avg(review_agent::int) {base}"), 6),
        (stat("PRs merged", f"SELECT count(*) {base}", unit="none"), 6),
    ], h=5)
    b.add([
        (timeseries("Share de PRs com IA por semana", f"SELECT merged_week AS time, "
                    f"avg(CASE WHEN {AI_GROUP} = 'Com IA' THEN 1 ELSE 0 END) AS \"PRs com IA\", "
                    f"avg(cloud_agent::int) AS \"PRs de agentes\", avg(review_agent::int) AS \"Review de IA\" "
                    f"{base} GROUP BY 1 ORDER BY 1", minv=0, maxv=1), 12),
        (timeseries("Throughput (PRs merged por semana)", f"SELECT merged_week AS time, {AI_GROUP} AS metric, count(*) AS value "
                    f"{base} GROUP BY 1,2 ORDER BY 1", unit="none", bars=True), 12),
    ], h=9)

    def cmp(metric, label, unit):
        return barchart(f"{label}: mediana e p75", f"SELECT {AI_GROUP} AS \"Grupo\", "
                        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY {metric}) AS \"Mediana\", "
                        f"percentile_cont(0.75) WITHIN GROUP (ORDER BY {metric}) AS \"p75\" "
                        f"{base} AND {metric} IS NOT NULL GROUP BY 1 ORDER BY 1", unit=unit)

    b.add([(cmp("cycle_time_h", "Cycle time", "h"), 8), (cmp("review_time_h", "Tempo até 1.ª review humana", "h"), 8),
           (cmp("batch_size", "Batch size (linhas +/−)", "none"), 8)], h=8)
    b.add([
        (timeseries("Cycle time mediano por semana", f"SELECT merged_week AS time, {AI_GROUP} AS metric, "
                    f"percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_time_h) AS value {base} GROUP BY 1,2 ORDER BY 1", unit="h"), 12),
        (table("Por ferramenta", f"SELECT COALESCE(t, 'Sem IA') AS \"Ferramenta\", count(*) AS \"PRs\", "
               f"percentile_cont(0.5) WITHIN GROUP (ORDER BY cycle_time_h) AS \"Cycle time (mediana)\", "
               f"percentile_cont(0.5) WITHIN GROUP (ORDER BY review_time_h) AS \"Review (mediana)\", "
               f"percentile_cont(0.5) WITHIN GROUP (ORDER BY batch_size) AS \"Batch size (mediana)\" "
               f"FROM v_pr_facts LEFT JOIN LATERAL unnest(NULLIF(ai_tools, '{{}}')) t ON TRUE "
               f"WHERE $__timeFilter(merged_at) AND {TEAM} GROUP BY 1 ORDER BY 2 DESC",
               desc="Um PR com duas ferramentas conta uma vez em cada (como no Swarmia).",
               units={"Cycle time (mediana)": "h", "Review (mediana)": "h"}), 12),
    ], h=9)
    b.row("Detalhe")
    b.add([(table("PRs e motivo da atribuição",
                  f"SELECT f.merged_at AS \"Merged\", f.repo || '#' || f.number AS \"PR\", f.title AS \"Título\", f.person AS \"Pessoa\", "
                  f"string_agg(a.tool || ' · ' || a.mode || ' · ' || a.confidence || ': ' || a.reason, E'\\n' ORDER BY a.tool) AS \"Atribuição\", "
                  f"f.cycle_time_h AS \"Cycle time\", f.batch_size AS \"Batch\" "
                  f"FROM v_pr_facts f LEFT JOIN pr_ai_attribution a ON a.pr_id = f.pr_id "
                  f"WHERE $__timeFilter(f.merged_at) AND f.{TEAM} GROUP BY f.pr_id, f.merged_at, f.repo, f.number, f.title, "
                  f"f.person, f.cycle_time_h, f.batch_size ORDER BY 1 DESC LIMIT 500", units={"Cycle time": "h"}), 24)], h=12)
    return b


def code() -> Board:
    b = Board("ai-code", "IA · Código gerado e qualidade", "% de código IA (Git AI), sobrevivência das linhas e change failure rate.",
              [high_only_var()])
    base = f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM} AND added_lines > 0"
    b.text("A **% de código IA** vem das notas do Git AI e só cobre commits feitos com o Git AI instalado — ver a cobertura ao lado "
           "antes de tirar conclusões. **Sobrevivência** = linhas de cada PR que ainda existem no ramo principal (git blame). "
           "Nunca olhar para a % de IA sem a sobrevivência ao lado.")
    b.add([
        (stat("% de código IA", f"SELECT sum(ai_lines)::numeric / NULLIF(sum(added_lines),0) {base}"), 6),
        (stat("Cobertura do Git AI", f"SELECT sum(commits_with_note)::numeric / NULLIF(sum(commit_count),0) "
              f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM}",
              desc="Commits com nota do Git AI ÷ commits. Abaixo de ~80% a % de IA não é representativa."), 6),
        (stat("Sobrevivência a 30+ dias (IA ≥50%)", f"SELECT avg(survival_rate) FROM v_survival WHERE age_days >= 30 "
              f"AND grupo = 'Maioria IA (>=50%)' AND {TEAM}"), 6),
        (stat("Sobrevivência a 30+ dias (sem IA)", f"SELECT avg(survival_rate) FROM v_survival WHERE age_days >= 30 "
              f"AND grupo = 'Sem IA' AND {TEAM}"), 6),
    ], h=5)
    b.add([
        (timeseries("% de código IA por semana", f"SELECT merged_week AS time, team AS metric, "
                    f"sum(ai_lines)::numeric / NULLIF(sum(added_lines),0) AS value {base} GROUP BY 1,2 ORDER BY 1", minv=0, maxv=1), 12),
        (table("Por repositório", f"SELECT repo AS \"Repo\", sum(ai_lines) AS \"Linhas IA\", sum(human_lines) AS \"Linhas humanas\", "
               f"sum(unknown_lines) AS \"Sem atestação\", sum(ai_lines)::numeric / NULLIF(sum(added_lines),0) AS \"% IA\", "
               f"sum(commits_with_note)::numeric / NULLIF(sum(commit_count),0) AS \"Cobertura\" "
               f"FROM v_pr_facts WHERE $__timeFilter(merged_at) AND {TEAM} GROUP BY 1 ORDER BY 2 DESC",
               units={"% IA": "percentunit", "Cobertura": "percentunit"}), 12),
    ], h=9)
    b.row("Qualidade")
    b.add([
        (barchart("Sobrevivência das linhas por idade do PR",
                  f"SELECT CASE WHEN age_days < 14 THEN '0–13 dias' WHEN age_days < 30 THEN '14–29 dias' "
                  f"WHEN age_days < 60 THEN '30–59 dias' WHEN age_days < 90 THEN '60–89 dias' ELSE '90+ dias' END AS \"Idade\", "
                  f"avg(survival_rate) FILTER (WHERE grupo = 'Maioria IA (>=50%)') AS \"Maioria IA\", "
                  f"avg(survival_rate) FILTER (WHERE grupo = 'Parte IA (<50%)') AS \"Parte IA\", "
                  f"avg(survival_rate) FILTER (WHERE grupo = 'Sem IA') AS \"Sem IA\" "
                  f"FROM v_survival WHERE {TEAM} GROUP BY 1 ORDER BY 1", unit="percentunit",
                  desc="Meia-vida = idade em que a sobrevivência cai abaixo de 50%. Retrabalho a 30 dias = 1 − sobrevivência aos 30–59 dias."), 12),
        (timeseries("Change failure rate (produção)", "SELECT week AS time, grupo AS metric, cfr AS value FROM v_cfr_weekly "
                    "WHERE $__timeFilter(week) ORDER BY 1", minv=0, maxv=1,
                    desc="Deploys com incidente ÷ deploys. 'Com IA' = o deploy inclui pelo menos um PR assistido (alta confiança)."), 12),
    ], h=9)
    return b


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for board in (adoption(), impact(), code()):
        (OUT / f"{board.uid}.json").write_text(json.dumps(board.json(), indent=2, ensure_ascii=False) + "\n")
        print("escrito", board.uid)


if __name__ == "__main__":
    main()
