# coach-mcp auth runbook

Secrets live only in `/opt/coach/.env` (mode 600). Never paste them in chat or commit them.

| Variable | What |
|---|---|
| `COACH_MCP_PUBLIC_URL` | `https://coach-mcp.lapolazzati.com` (origin only, no `/mcp`) |
| `COACH_MCP_ALLOWED_GITHUB_ID` | your numeric GitHub id (`curl https://api.github.com/users/<login>` → `id`) |
| `COACH_MCP_GITHUB_CLIENT_ID` / `_SECRET` | GitHub OAuth app, callback `https://coach-mcp.lapolazzati.com/auth/callback` |
| `COACH_MCP_JWT_KEY` / `COACH_MCP_STORAGE_KEY` | random, ≥32 chars: `python3 -c "import secrets; print(secrets.token_urlsafe(48))"` |

After any change: `cd /opt/coach/app && docker compose --env-file /opt/coach/.env up -d --force-recreate coach-mcp`.

## Rotate the GitHub client secret
GitHub → Settings → Developer settings → OAuth Apps → coach → Generate new secret.
Put it in `.env`, recreate `coach-mcp`. Existing coach tokens keep working until they expire.

## Revoke everything
1. `docker compose stop coach-mcp`
2. Wipe `/opt/coach/data/mcp-auth/`
3. Rotate `COACH_MCP_JWT_KEY` and `COACH_MCP_STORAGE_KEY`
4. `docker compose ... up -d --force-recreate coach-mcp`
5. In the Claude app the connector shows as disconnected: Settings → Connectors → coach → Connect, log in with GitHub again.
