# Public exposure of coach-mcp (cloudflared)

Only `coach-mcp` is public. The dashboard stays on the Tailscale IP.

1. DNS record. `~/.cloudflared/cert.pem` is for the `catone.ai` zone, so
   `cloudflared tunnel route dns` can NOT create records in `lapolazzati.com` (it would create
   `coach-mcp.lapolazzati.com.catone.ai`). Add it in the Cloudflare dashboard, zone
   `lapolazzati.com`: `CNAME coach-mcp → adb27196-4f0b-4853-8f78-2ce4626168d9.cfargotunnel.com`, proxied.

2. Ingress rule in `/etc/cloudflared/config.yml` (root), above the final `http_status:404`:

   ```yaml
     - hostname: coach-mcp.lapolazzati.com
       service: http://127.0.0.1:9091
   ```

3. `sudo cloudflared --config /etc/cloudflared/config.yml tunnel ingress validate && sudo systemctl restart cloudflared`

4. Check from outside: `curl -i https://coach-mcp.lapolazzati.com/mcp` → `401` with
   `WWW-Authenticate: Bearer ... resource_metadata=...`.

Optional hardening (Cloudflare dashboard, KTD3): a rate-limit rule on the hostname,
tighter on `/register` and `/token`; a WAF rule limiting `/mcp` to Anthropic egress
(`160.79.104.0/21`, `153.61.208.0/20`) while leaving the OAuth endpoints open.
