# Remote access via Cloudflare Tunnel

Exposes the wildlife-detector web UI (currently on `http://localhost:8200`) as
a public HTTPS URL through a Cloudflare Tunnel + Access. **No router port
forwarding required**; the tunnel is an outbound connection from the host to
Cloudflare's edge.

Pattern: **outbound reverse-proxy tunnel + identity-aware gate at the trust
boundary** (Cloudflare Access) — the app itself never sees an unauthenticated
request from the internet.

## One-time Cloudflare dashboard setup

1. **Create the tunnel**
   - Cloudflare dashboard → **Zero Trust** → **Networks** → **Tunnels**
   - **Create a tunnel** → connector `Cloudflared` → name it `wildlife-detector`
   - After creation, the dashboard shows install commands. **Copy the token
     from the Docker line** — it's the long `eyJhIjo...` string. That's the
     only thing you need; the rest of the install command is redundant
     because this repo's `docker-compose.yml` runs cloudflared itself.

2. **Add a public hostname**
   - Same tunnel → **Public hostnames** tab → **Add a public hostname**
   - Subdomain: `wildlife` (or whatever you want)
   - Domain: `danhle.net` (or any zone you own on Cloudflare)
   - Service type: `HTTP`
   - Service URL: `wildlife-web:8100`  ← the docker-compose service name,
     NOT `localhost` or a host port. The tunnel container joins the same
     compose network and reaches `wildlife-web` by service name.

3. **Wrap it with Access (auth) — do not skip**
   - **Zero Trust** → **Access** → **Applications** → **Add an application**
   - Type: `Self-hosted`
   - Application domain: same subdomain + domain from step 2
   - **Add policy** → name `owner` → Rule action: `Allow` → include:
     `Emails` → your email(s)
   - Save. Without this step, the tunnel would expose the UI to the entire
     internet with no auth. That would be very bad.

## Local setup

1. Paste the token from step 1 into `.env`:
   ```
   TUNNEL_TOKEN=eyJhIjo...
   ```

2. Bring up the `cloudflared` container using the `remote-access` compose
   profile (the profile keeps this optional — home-lab-only setups aren't
   forced to install cloudflared):
   ```powershell
   docker compose --profile remote-access up -d cloudflared
   ```

3. Visit your public URL from anywhere. Cloudflare Access will prompt for
   an email login on first hit; a one-time code goes to your inbox.

## Operating notes

- **Reload after hostname changes**: dashboard hostname edits don't require
  a container restart, but a `docker compose restart cloudflared` picks up
  any latency-improving edge routing changes.

- **Logs**: `docker logs -f wildlife-cloudflared` shows tunnel connection
  events. First few seconds after startup will show `Registered tunnel
  connection` lines — that's success.

- **Tunnel down = local still works**: cloudflared is outbound-only, so a
  tunnel outage only affects remote access. `http://localhost:8200` on the
  LAN keeps working.

- **Removing the token**: `docker compose --profile remote-access down
  cloudflared` stops the tunnel; the public URL will start returning 502
  within a few seconds.

- **Session length**: Access defaults to 24h. Change under Zero Trust
  → Settings → Authentication if you want longer/shorter.

## Security posture

- **Auth surface** = Cloudflare Access (email → one-time code by default;
  can add TOTP/SSO/etc). The wildlife-web app itself has no auth of its own
  — Access does the entire trust-boundary job.
- **Blast radius if the token leaks**: someone can bring up their own
  `cloudflared` connector with your token and proxy your traffic. Rotate
  the token from the dashboard if you suspect this.
- **Blast radius if the Access policy is misconfigured** (e.g., `Everyone`):
  the UI is world-open. Double-check the policy after setup.
