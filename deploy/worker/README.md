# screen-gw — Worker gateway (you deploy it; nothing here has been applied)

Separate Worker from the other agents' gateways. It only rate-limits, checks API keys and forwards. It logs nothing
(screened names pass through the Worker in request bodies that are never read, only size-checked).

## One-time setup (≈10 minutes)

1. On your laptop: `cd D:\Agents\sanctions-screener\deploy\worker` and `npm i -g wrangler@4` if you don't have it (or `npx wrangler`).
2. `wrangler login` → browser → **Allow** (your account; Claude never does this).
3. The API-key store `ORACLE_KEYS` (id `6bec4f9b…`) already exists from the Oracle — one key works for every agent.
4. `wrangler deploy` → it prints the route `screen.noahsarch.tech/*`.
   The tunnel hostname (deploy-agent.bat, ingress prompt) must already exist, otherwise Cloudflare has no origin.
5. Test:
   ```
   curl -s https://screen.noahsarch.tech/v1/health
   curl -s -X POST https://screen.noahsarch.tech/v1/tools/list_status -H "content-type: application/json" -d "{}"
   curl -s -X POST https://screen.noahsarch.tech/v1/tools/explain_match -H "content-type: application/json" -d "{\"name_a\":\"Mohamed Hussein\",\"name_b\":\"محمد حسين\"}"
   curl -s -X POST https://screen.noahsarch.tech/v1/tools/screen_name -H "content-type: application/json" -d "{\"name\":\"Ali Kony\"}"   # → 402 without a key
   ```

## Issuing an API key (per customer)

```
wrangler kv key put --remote --binding ORACLE_KEYS "<generate a 32+ char random key>" '{"plan":"paid","label":"customer-name"}'
```
Revoke: `wrangler kv key delete --binding ORACLE_KEYS "<key>"`. Keys are hashed before they touch the rate limiter and are
stripped before the request reaches your server, so your server never sees or stores them.

## Knobs (`wrangler.toml` → `[vars]`)

| var | meaning | default |
|---|---|---|
| `FREE_TOOLS` | REST tools callable without a key | list_status, explain_match |
| `FREE_LIMIT_PER_MINUTE` / `RL_FREE` | per-client limit without a key | 30/min |
| `KEYED_LIMIT_PER_MINUTE` / `RL_KEYED` | per-key limit | 600/min |
| `REQUIRE_KEY_FOR_MCP` | gate the whole `/mcp` endpoint behind a key | false (flip once keys exist) |
| `MAX_BODY_BYTES` | reject bigger bodies (a 500-row batch is ~80 KB) | 262144 |

Rate-limit namespace ids 2181 (free) / 2182 (keyed) are unique to this Worker; the Rate Limiting binding is Cloudflare's
"Rate Limiting API" (free Workers plan). If `wrangler deploy` complains, update Wrangler (`npm i -g wrangler@latest`).
