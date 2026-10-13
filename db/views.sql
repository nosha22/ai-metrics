-- Outsmart AI metrics — views de métricas
-- Recriadas sempre que o schema é aplicado.

DROP VIEW IF EXISTS v_cfr_weekly, v_survival, v_pr_facts, v_git_ai_pr, v_idle_seats,
    v_adoption_weekly, v_ai_activity, v_members CASCADE;

-- Membros atuais (como o Swarmia: a composição atual da equipa vale para todo o histórico)
CREATE VIEW v_members AS
SELECT p.id AS person_id, p.name, COALESCE(t.name, '(no team)') AS team
FROM people p
LEFT JOIN teams t ON t.id = p.team_id
WHERE NOT p.is_bot AND (p.left_on IS NULL OR p.left_on > current_date);

-- Atividade diária por pessoa e ferramenta. Se a mesma ferramenta reporta pela Analytics API
-- e por OpenTelemetry no mesmo dia, fica a fonte com mais uso (regra do Swarmia).
CREATE VIEW v_ai_activity AS
SELECT DISTINCT ON (i.person_id, a.tool, a.day)
    i.person_id, a.tool, a.day, a.source, a.is_active, a.sessions,
    a.lines_added, a.lines_removed, a.actions_accepted, a.actions_rejected,
    a.chat_requests, a.agent_requests, a.commits, a.pull_requests, a.tokens, a.cost_usd,
    a.last_activity_at
FROM ai_daily_activity a
JOIN identities i ON i.id = a.identity_id
WHERE i.person_id IS NOT NULL
ORDER BY i.person_id, a.tool, a.day,
         (a.lines_added + a.actions_accepted + a.chat_requests + a.agent_requests) DESC,
         a.tokens DESC;

-- Adoção por equipa, ferramenta e semana (definições do Swarmia):
--   enabled  = membros com licença em algum momento da semana (ou ativos, quando a fonte não dá licenças)
--   active   = membros que aceitaram código ou usaram chat na semana
CREATE VIEW v_adoption_weekly AS
WITH weeks AS (
    SELECT generate_series(date_trunc('week', current_date - 364), date_trunc('week', current_date), '1 week')::date AS week
),
tools AS (
    SELECT DISTINCT tool FROM ai_licenses UNION SELECT DISTINCT tool FROM ai_daily_activity
),
grid AS (
    SELECT m.person_id, m.team, t.tool, w.week FROM v_members m CROSS JOIN tools t CROSS JOIN weeks w
),
lic AS (
    SELECT g.person_id, g.tool, g.week
    FROM grid g JOIN ai_licenses l
      ON l.person_id = g.person_id AND l.tool = g.tool
     AND l.valid_from <= g.week + 6 AND (l.valid_to IS NULL OR l.valid_to >= g.week)
),
act AS (
    SELECT person_id, tool, date_trunc('week', day)::date AS week
    FROM v_ai_activity WHERE is_active GROUP BY 1,2,3
)
SELECT g.week, g.team, g.tool,
       count(DISTINCT g.person_id)                                              AS members,
       count(DISTINCT COALESCE(lic.person_id, act.person_id))                   AS enabled,
       count(DISTINCT act.person_id)                                            AS weekly_active
FROM grid g
LEFT JOIN lic ON lic.person_id = g.person_id AND lic.tool = g.tool AND lic.week = g.week
LEFT JOIN act ON act.person_id = g.person_id AND act.tool = g.tool AND act.week = g.week
GROUP BY 1,2,3;

-- Licenças paradas: licença ativa hoje e sem atividade nos últimos 28 dias
CREATE VIEW v_idle_seats AS
SELECT m.team, m.name, l.tool, l.valid_from,
       (SELECT max(day) FROM v_ai_activity a WHERE a.person_id = l.person_id AND a.tool = l.tool AND a.is_active) AS last_active_day
FROM ai_licenses l
JOIN v_members m ON m.person_id = l.person_id
WHERE (l.valid_to IS NULL OR l.valid_to >= current_date)
  AND NOT EXISTS (
      SELECT 1 FROM v_ai_activity a
      WHERE a.person_id = l.person_id AND a.tool = l.tool AND a.is_active AND a.day >= current_date - 28);

-- Git AI agregado por PR
CREATE VIEW v_git_ai_pr AS
SELECT c.pr_id,
       count(*)                                    AS commits,
       count(*) FILTER (WHERE s.has_note)          AS commits_with_note,
       COALESCE(sum(s.ai_additions), 0)            AS ai_lines,
       COALESCE(sum(s.human_additions), 0)         AS human_lines,
       COALESCE(sum(s.unknown_additions), 0)       AS unknown_lines,
       COALESCE(sum(s.git_diff_added_lines), 0)    AS added_lines
FROM commits c
LEFT JOIN git_ai_commit_stats s ON s.repo_id = c.repo_id AND s.sha = c.sha
WHERE c.pr_id IS NOT NULL
GROUP BY c.pr_id;

-- Uma linha por PR com tudo o que os dashboards precisam
CREATE VIEW v_pr_facts AS
SELECT pr.id AS pr_id, r.full_name AS repo, pr.number, pr.title,
       COALESCE(tm.name, rt.name, '(no team)') AS team,
       p.name AS person,
       pr.created_at, pr.merged_at,
       date_trunc('week', pr.merged_at)::date AS merged_week,
       EXTRACT(EPOCH FROM pr.merged_at - COALESCE(pr.first_commit_at, pr.created_at)) / 3600.0 AS cycle_time_h,
       EXTRACT(EPOCH FROM pr.first_review_at - pr.created_at) / 3600.0                     AS review_time_h,
       pr.additions + pr.deletions AS batch_size,
       EXISTS (SELECT 1 FROM pr_ai_attribution a WHERE a.pr_id = pr.id AND a.mode = 'assisted')                          AS ai_assisted,
       EXISTS (SELECT 1 FROM pr_ai_attribution a WHERE a.pr_id = pr.id AND a.mode = 'assisted' AND a.confidence = 'high') AS ai_assisted_high,
       EXISTS (SELECT 1 FROM pr_ai_attribution a WHERE a.pr_id = pr.id AND a.mode = 'cloud_agent')                       AS cloud_agent,
       EXISTS (SELECT 1 FROM pr_ai_attribution a WHERE a.pr_id = pr.id AND a.mode = 'review_agent')                      AS review_agent,
       ARRAY(SELECT DISTINCT a.tool FROM pr_ai_attribution a WHERE a.pr_id = pr.id AND a.mode <> 'review_agent' AND a.tool <> 'git_ai' ORDER BY 1) AS ai_tools,
       g.ai_lines, g.human_lines, g.unknown_lines, g.added_lines,
       g.commits_with_note, g.commits AS commit_count,
       CASE WHEN g.added_lines > 0 THEN g.ai_lines::numeric / g.added_lines END AS ai_share
FROM pull_requests pr
JOIN repositories r ON r.id = pr.repo_id
LEFT JOIN teams rt ON rt.id = r.team_id
LEFT JOIN people p ON p.id = pr.initiated_by
LEFT JOIN teams tm ON tm.id = p.team_id
LEFT JOIN v_git_ai_pr g ON g.pr_id = pr.id
WHERE pr.merged_at IS NOT NULL;

-- Sobrevivência das linhas por PR: todas as medições, com a idade do PR na data da medição
CREATE VIEW v_survival AS
SELECT f.pr_id, f.repo, f.team, f.merged_at, s.measured_at,
       (s.measured_at - f.merged_at::date) AS age_days,
       s.surviving_lines, pr.additions,
       LEAST(1.0, s.surviving_lines::numeric / NULLIF(pr.additions, 0)) AS survival_rate,
       f.ai_share,
       CASE WHEN f.ai_share IS NULL THEN 'No Git AI data'
            WHEN f.ai_share >= 0.5 THEN 'Mostly AI (>=50%)'
            WHEN f.ai_share > 0 THEN 'Partly AI (<50%)'
            ELSE 'No AI' END AS ai_group
FROM line_survival s
JOIN pull_requests pr ON pr.id = s.pr_id
JOIN v_pr_facts f ON f.pr_id = s.pr_id
WHERE pr.additions > 0;

-- Change failure rate por semana, separando deploys com e sem PRs assistidos por IA
CREATE VIEW v_cfr_weekly AS
WITH d AS (
    SELECT dep.id, date_trunc('week', dep.deployed_at)::date AS week,
           bool_or(f.ai_assisted_high) AS has_ai,
           EXISTS (SELECT 1 FROM incidents i WHERE i.caused_by_deployment = dep.id) AS failed
    FROM deployments dep
    LEFT JOIN deployment_prs dp ON dp.deployment_id = dep.id
    LEFT JOIN v_pr_facts f ON f.pr_id = dp.pr_id
    WHERE dep.environment = 'production'
    GROUP BY dep.id, dep.deployed_at
)
SELECT week,
       CASE WHEN COALESCE(has_ai, FALSE) THEN 'With AI' ELSE 'Without AI' END AS ai_group,
       count(*) AS deployments,
       count(*) FILTER (WHERE failed) AS failed,
       count(*) FILTER (WHERE failed)::numeric / NULLIF(count(*), 0) AS cfr
FROM d GROUP BY 1, 2;
