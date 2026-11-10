#!/bin/bash
# Set up a *named* Cloudflare Tunnel for this app, with DNS, so the site gets a stable
# HTTPS URL on your own domain instead of a random trycloudflare.com hostname.
#
# Prereqs:
#   1. A free Cloudflare account, and the domain added to it (nameservers moved to Cloudflare).
#   2. An API token with these permissions:
#        Account  -> Cloudflare Tunnel : Edit
#        Zone     -> DNS               : Edit
#        Zone     -> Zone              : Read
#      (My Profile -> API Tokens -> Create Token -> Custom token)
#   3. cloudflared installed at /usr/local/bin/cloudflared
#
# Usage:
#   sudo CF_API_TOKEN=xxx CF_ZONE=example.com CF_HOST=manga.example.com ./setup_cf_tunnel.sh
#
# Optional: TUNNEL_NAME (default manga-to-pdf-tunnel), TARGET (default http://127.0.0.1:8099)
set -euo pipefail

: "${CF_API_TOKEN:?set CF_API_TOKEN}"
: "${CF_ZONE:?set CF_ZONE (e.g. example.com)}"
: "${CF_HOST:?set CF_HOST (e.g. manga.example.com)}"
TUNNEL_NAME="${TUNNEL_NAME:-manga-to-pdf-tunnel}"
TARGET="${TARGET:-http://127.0.0.1:8099}"
API="https://api.cloudflare.com/client/v4"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

cf() {  # cf METHOD PATH [BODY]
    local method="$1" path="$2" body="${3:-}"
    if [ -n "$body" ]; then
        curl -sS -X "$method" "$API$path" \
            -H "Authorization: Bearer $CF_API_TOKEN" \
            -H "Content-Type: application/json" \
            --data-binary "$body"
    else
        curl -sS -X "$method" "$API$path" -H "Authorization: Bearer $CF_API_TOKEN"
    fi
}

jget() {  # jget JSON_PATH   (reads stdin)
    python3 -c 'import json,sys
d=json.load(sys.stdin)
for k in sys.argv[1].split("."):
    if k == "": continue
    if isinstance(d, list): d = d[int(k)]
    else: d = d.get(k)
    if d is None: break
print("" if d is None else d)' "$1"
}

command -v cloudflared >/dev/null || die "cloudflared not found in PATH"
command -v python3 >/dev/null || die "python3 not found"

say "1/6 verify token and locate account / zone"
ACCT=$(cf GET /accounts | jget "result.0.id")
[ -n "$ACCT" ] || die "cannot read accounts with this token (needs Cloudflare Tunnel:Edit)"
ZONE=$(cf GET "/zones?name=$CF_ZONE" | jget "result.0.id")
[ -n "$ZONE" ] || die "zone $CF_ZONE not found (needs Zone:Read, and the domain must be on Cloudflare)"
echo "account=$ACCT"
echo "zone=$ZONE"

say "2/6 create or reuse the tunnel"
TID=$(cf GET "/accounts/$ACCT/cfd_tunnel?name=$TUNNEL_NAME&is_deleted=false" | jget "result.0.id")
if [ -z "$TID" ]; then
    TID=$(cf POST "/accounts/$ACCT/cfd_tunnel" \
        "{\"name\":\"$TUNNEL_NAME\",\"config_src\":\"cloudflare\"}" | jget "result.id")
    echo "created tunnel $TID"
else
    echo "reusing tunnel $TID"
fi
[ -n "$TID" ] || die "tunnel creation failed"

say "3/6 set ingress (hostname -> $TARGET)"
cf PUT "/accounts/$ACCT/cfd_tunnel/$TID/configurations" \
    "{\"config\":{\"ingress\":[{\"hostname\":\"$CF_HOST\",\"service\":\"$TARGET\"},{\"service\":\"http_status:404\"}]}}" \
    | jget "success" >/dev/null
echo "ingress ok"

say "4/6 create DNS record (proxied CNAME -> $TID.cfargotunnel.com)"
SUB="${CF_HOST%.$CF_ZONE}"
EXIST=$(cf GET "/zones/$ZONE/dns_records?name=$CF_HOST" | jget "result.0.id")
if [ -n "$EXIST" ]; then
    echo "DNS record already exists ($EXIST), leaving it alone"
else
    cf POST "/zones/$ZONE/dns_records" \
        "{\"type\":\"CNAME\",\"name\":\"$SUB\",\"content\":\"$TID.cfargotunnel.com\",\"proxied\":true,\"ttl\":1}" \
        | jget "success" >/dev/null
    echo "DNS record created"
fi

say "5/6 install the connector as a systemd service"
TOKEN=$(cf GET "/accounts/$ACCT/cfd_tunnel/$TID/token" | jget "result")
[ -n "$TOKEN" ] || die "could not fetch tunnel token"

install -d -m 700 /etc/cloudflared
umask 077
printf 'TUNNEL_TOKEN=%s\n' "$TOKEN" > /etc/cloudflared/token.env
chmod 600 /etc/cloudflared/token.env

# stop the old quick tunnel if present
systemctl disable --now manga-tunnel >/dev/null 2>&1 || true

cat > /etc/systemd/system/manga-tunnel.service <<'UNIT'
[Unit]
Description=Cloudflare Tunnel for manga-to-pdf
After=network-online.target manga.service
Wants=manga.service

[Service]
Type=simple
User=root
EnvironmentFile=/etc/cloudflared/token.env
ExecStart=/usr/local/bin/cloudflared --no-autoupdate tunnel run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now manga-tunnel
echo "service started"

say "6/6 verify"
sleep 8
systemctl is-active manga-tunnel
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 25 "https://$CF_HOST/api/status" || true)
echo "https://$CF_HOST/api/status -> ${CODE:-no response}"
printf '\nDone. Site: https://%s/\n' "$CF_HOST"
