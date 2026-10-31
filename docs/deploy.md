# Deploy — GCC Sanctions & Terror-List Screener

Same flow as every agent (see `shared/README.md`). Host `screen.noahsarch.tech`, ports **8785 MCP / 8786 REST**
(add the row to `shared/docs/registry.md`; the registry's "next agent" placeholder said 8783–8784 — this agent was assigned
8785/8786 by the brief, leave 8783/8784 for the next one or reconcile).

One command from Windows does git push → DNS (if `CF_API_TOKEN`/`CF_ZONE_ID` are in `server.env`) → server deploy →
Worker → public checks:

```
D:\Agents\shared\deploy\windows\release-agent.bat sanctions-screener
```

Manual pieces if needed:

1. DNS: CNAME `screen` → `72a55974-9cb4-4cae-889b-35a9522f14ce.cfargotunnel.com` (proxied).
2. Server: `D:\Agents\shared\deploy\windows\deploy-agent.bat sanctions-screener` (answer `y` to the ingress prompt).
   `install.sh` also installs the cron line from `AGENT_CRON_JOBS` (Mondays 03:17):
   `docker compose run --rm -T screen-refresh && docker compose restart screen-mcp screen-api`.
3. Worker: `cd D:\Agents\sanctions-screener\deploy\worker` → `wrangler deploy` (rate-limit namespaces 2181/2182, shared KV `ORACLE_KEYS`).
4. Apify: Console → Actors → Develop new → link GitHub `mohamadnajiya/sanctions-screener` → Build → Settings:
   Standby on, **512 MB** (the index needs ~210 MB), 32/24 → Publishing → Monetization: pay-per-event, remove the default
   dataset event, add `name-screened` "Name screened" $0.01 (primary), `audit-report` "Audit report" $0.25,
   `list-status` "List status" $0 → Confirm → Display information (title/description ≤ 300 chars, see `.actor/actor.json`)
   → Publish on Store.

## Data refresh (the only networked step)

- Locally before a release: `uv run screen refresh` (≈ 170 MB of downloads, ~1 min) → review `git diff --stat data/` →
  commit as `data: lists 2026-11-07` → release. The image and the Actor build from the committed snapshot.
- On the server: the weekly cron above rewrites `~/agents/sanctions-screener/data` (bind-mounted) and restarts the two
  services (startup ≈ 5 s to build the index). A failed list keeps its previous file and is reported in `version.yaml`
  → `failed` and in `list_status` → `last_refresh_failures` / `warnings`.
- A list older than 7 days turns `data_confidence` to `medium` and adds a warning to every response, so customers see it.
- Run it daily instead if a customer's regulator expects daily screening: change the cron expression in `deploy/agent.env`.

Outbound: the refresh container needs HTTPS to sanctionslistservice.ofac.treas.gov (redirects to an S3 us-gov-west-1
presigned URL), scsanctions.un.org, webgate.ec.europa.eu, sanctionslist.fcdo.gov.uk and www.uaeiec.gov.ae. The request
path needs no outbound access at all.

Test (replace the key):

```
curl.exe -s https://screen.noahsarch.tech/v1/health
curl.exe -s -X POST https://screen.noahsarch.tech/v1/tools/list_status -H "content-type: application/json" -d "{}"
curl.exe -s -X POST https://screen.noahsarch.tech/v1/tools/screen_name -H "content-type: application/json" -H "X-API-Key: <key>" -d '{\"name\":\"Qusay Saddam Hussein\",\"dob\":\"1966\",\"nationality\":\"IQ\"}'
```

Local run: `uv run screen-mcp --transport http` (8785) · `uv run screen-api` (8786) ·
`uv run screen query screen_name '{"name":"Ali Kony"}'` · `uv run screen validate` · `uv run screen refresh --only uae`.
