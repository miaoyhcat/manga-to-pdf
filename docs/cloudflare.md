# Cloudflare deployment

The application needs a real Python process (downloaders + image pipeline), so it **cannot run
on Cloudflare Pages or Workers**. Pages is static-only and Workers cannot spawn the binaries or
keep the disk-backed job queue.

What Cloudflare is used for here is the **front door**: an outbound-only tunnel that gives the
app a stable HTTPS hostname even when the cloud provider blocks inbound 80/443.

```
user ──HTTPS──> Cloudflare edge ──tunnel(outbound)──> cloudflared ──> 127.0.0.1:8099
```

Nothing needs to be reachable from the internet on the server itself.

## Why this is sometimes the only option

Some providers (Tencent Cloud in particular) close inbound web ports for addresses without a
valid ICP filing. Symptom: SSH works, `curl http://127.0.0.1/manga/` works locally, but the
public IP times out on 80/443 from everywhere.

Check:

```bash
# on the server (should be 200)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8099/

# from outside (000 = blocked upstream of the machine)
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://<server-ip>:80/
```

## Option A — quick tunnel (no account, random URL)

```bash
cloudflared tunnel --no-autoupdate --url http://127.0.0.1:8099
```

Gives `https://<random>.trycloudflare.com`. Zero setup, but the hostname changes on every
restart, so it is only good for a temporary bridge.

## Option B — named tunnel (stable hostname, needs an account)

1. Create a free Cloudflare account and add your domain (change its nameservers to the ones
   Cloudflare shows you).
2. Create an API token: *My Profile → API Tokens → Create Token → Custom token*
   - Account → **Cloudflare Tunnel: Edit**
   - Zone → **DNS: Edit**
   - Zone → **Zone: Read**
3. Run the helper:

```bash
sudo CF_API_TOKEN=... CF_ZONE=example.com CF_HOST=manga.example.com \
     bash scripts/setup_cf_tunnel.sh
```

It creates the tunnel, wires `manga.example.com` to `http://127.0.0.1:8099`, adds the proxied
CNAME, installs `manga-tunnel.service`, and prints the resulting URL.

## Cloudflare Pages (optional)

Pages can host a **launcher/landing page** only. A page served from Pages is HTTPS, and browsers
block HTTPS pages from calling an HTTP API, so the app itself must already be served over HTTPS
(Option A or B). With a named tunnel, point the launcher at `https://manga.example.com/`.

## Notes

- Keep the tunnel token in `/etc/cloudflared/token.env` (mode 600); it is a credential.
- `cloudflared` logs: `journalctl -u manga-tunnel -f`
- Free plan is fine: one tunnel, a handful of hostnames, no bandwidth limit for tunnels.
