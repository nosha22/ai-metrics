# AI metrics — Outsmart

Métricas de adoção e impacto de IA na engenharia da Outsmart, ao estilo do Swarmia, alojadas no servidor interno.

Responde a quatro perguntas:

| Pergunta | Dashboard | Fonte |
| --- | --- | --- |
| Quem usa as ferramentas de IA? | **IA · Adoção** | Claude Code (Analytics API ou OpenTelemetry), Cursor Admin API, `organization.yml` |
| Como usa? | **IA · Adoção** → Atividade | as mesmas |
| Que efeito tem nos PRs? | **IA · Impacto nos PRs** | GitHub (PRs, commits, reviews) + regras de deteção |
| Quanto do código é IA, e aguenta-se? | **IA · Código gerado e qualidade** | notas do Git AI, `git blame`, deploys e incidentes |

As regras de deteção e as definições das métricas seguem a documentação pública do Swarmia
([deteção](https://help.swarmia.com/features/ai-tools/ai-tool-detection-and-filters),
[adoção](https://help.swarmia.com/features/ai-tools/ai-adoption)). Estão em `aimetrics/detection.py` e `db/views.sql`.

## Arquitetura

```
GitHub API ─┐
Claude API ─┤                     ┌──────────────┐
Cursor API ─┼─► scheduler (diário)─►              │
Git AI     ─┘                     │   Postgres   ├──► Grafana (3 dashboards)
Claude Code ─OTLP─► api (:8080) ──►  factos +    │
CI deploys ──webhook─► api ───────►   views      │
                                  └──────────────┘
```

* `scheduler` corre `aimetrics run` todos os dias às 03:07 UTC (e ao arrancar).
* `api` recebe a telemetria do Claude Code e os eventos de deploy/incidente.
* As fontes só gravam factos brutos; a atribuição e as métricas são calculadas por cima. Uma regra nova recalcula o histórico com `aimetrics attribute --all`.

## Instalação no servidor

```bash
git clone https://github.com/nosha22/ai-metrics.git && cd ai-metrics
cp .env.example .env                                  # preencher (valores no 1Password)
cp config/organization.example.yml config/organization.yml   # equipas, pessoas, repos
docker compose up -d --build
```

Grafana em `http://<servidor>:3000` (utilizador `admin`, password `GRAFANA_ADMIN_PASSWORD`), pasta **IA**.
Pôr o Grafana e a porta 8080 atrás do reverse proxy com HTTPS antes de ligar o OpenTelemetry.

Comandos úteis (dentro do contentor `scheduler`):

```bash
docker compose exec scheduler python -m aimetrics run            # recolha completa agora
docker compose exec scheduler python -m aimetrics sync github --days 180   # backfill
docker compose exec scheduler python -m aimetrics org unlinked   # contas sem pessoa associada
docker compose exec scheduler python -m aimetrics attribute --all
```

## Ligar as fontes

### Fase 1 — GitHub (obrigatório)

Fine-grained token **só de leitura** nos repos a medir: *Pull requests*, *Contents*, *Deployments*, *Metadata*.
Pôr em `GITHUB_TOKEN` e listar os repos em `organization.yml`.

Para as regras de alta confiança funcionarem, as ferramentas têm de manter a atribuição nos commits:

* **Claude Code**: não desligar as [attribution settings](https://code.claude.com/docs/en/settings#attribution-settings)
  (`Co-Authored-By: Claude <noreply@anthropic.com>`).
* **Cursor**: manter o trailer `Made-with: Cursor`.

### Fase 2 — Claude Code e Cursor

**Claude Code** — depende de como a equipa paga:

| Plano | Como ligar |
| --- | --- |
| Claude Console (API) | Admin API key em `ANTHROPIC_ADMIN_KEY`. Dá 30 dias de histórico. |
| Team / Enterprise (subscrição) | OpenTelemetry: copiar `deploy/claude-code/managed-settings.json` para as managed settings em claude.ai (admin → Claude Code), com o URL público da `api` e o `OTEL_INGEST_TOKEN`. Sem histórico; não mostra licenças paradas. |

As duas podem coexistir; quando a mesma pessoa aparece nas duas no mesmo dia, conta a fonte com mais uso.
O recetor só aceita `http/json` com temporalidade `delta` (também converte `cumulative`).

**Cursor** — Admin API key em `CURSOR_ADMIN_KEY`. Os membros da equipa Cursor passam a ser as licenças
(`--no-licenses` para gerir as licenças só no YAML).

Depois de ligar, correr `aimetrics org unlinked` e acrescentar as contas em falta ao `organization.yml`.
Emails iguais entre fontes ligam-se sozinhos.

### Fase 3 — Git AI (% de código IA)

1. Instalar o Git AI nas máquinas da equipa: `curl -sSL https://usegitai.com/install.sh | bash`
   (sem chave de telemetria: a atribuição fica só nas notas do git).
2. Em cada repo, adicionar `deploy/github/git-ai.yaml` como `.github/workflows/git-ai.yaml`
   (ou correr `git ai ci github install`). Sem isto, o *squash merge* do GitHub perde as notas.
3. O `scheduler` faz clone dos repos (sem checkout), lê `refs/notes/ai` com `git ai stats <sha> --json`
   e mede a sobrevivência das linhas de cada PR com `git blame`, uma vez por semana, durante 180 dias.

O dashboard mostra a **cobertura** (commits com nota ÷ commits). Abaixo de ~80% a % de IA não é representativa.

### Fase 4 — Deploys e incidentes (change failure rate)

* **Deploys**: se usam a API de Deployments do GitHub, o coletor importa-os sozinho. Senão, acrescentar
  `deploy/github/report-deployment.yaml` ao workflow de deploy.
* **Incidentes**: `POST /events/incident` com `EVENTS_TOKEN`:

```bash
curl -X POST https://<api>/events/incident -H "Authorization: Bearer $EVENTS_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"id":"INC-42","title":"Login falha","opened_at":"2026-10-13T10:00:00Z","deployment_id":"nosha22/outsmart-api-123"}'
```

## Regras de deteção

| Modo | Regra | Confiança |
| --- | --- | --- |
| Assistido | Commit escrito ou co-escrito por IA (`Co-Authored-By`, autor do commit) | Alta |
| Assistido | Trailer da ferramenta (`Made-with: Cursor`, `Claude-Session:`) | Alta |
| Assistido | Label `claude-code-assisted` no PR | Alta |
| Assistido | Git AI registou linhas IA nos commits do PR | Alta |
| Assistido | Autor de um commit usou a ferramenta nas 24 h anteriores | Baixa |
| Agente | PR ou 1.º commit criado por um agente (`claude[bot]`, `cursoragent`, …) | Alta |
| Review de IA | Review de `claude[bot]`, `cursor[bot]`, … | Alta |

O filtro **Só alta confiança** nos dashboards desliga a regra das 24 h.
Padrões extra vão para `detection:` no `organization.yml`. Cada PR guarda o motivo da atribuição.

## Desenvolvimento

```bash
pip install -e ".[dev]"
pytest -q                                            # testes unitários
TEST_DATABASE_URL=postgresql://... pytest -q          # + teste ponta a ponta com Postgres
python scripts/build_dashboards.py                   # os dashboards são gerados a partir deste script
```

## Privacidade

* Os dashboards mostram equipas; dados por pessoa só existem nas tabelas de licenças paradas e no detalhe de PRs.
* Não se guarda conteúdo de prompts nem código: só contagens, datas e metadados de commits/PRs.
* Antes de ligar as fontes, partilhar com a equipa o que é recolhido e para quê.
