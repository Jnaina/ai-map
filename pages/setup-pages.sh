#!/usr/bin/env bash
# One-time setup of the Cloudflare Pages project + D1 database + secrets + custom domain
# for Jag's AI Map. Re-runnable; each step tolerates "already exists".
#
# PREREQUISITES (provide auth one of two ways):
#   a) interactive:  npx wrangler login
#   b) token:        export CLOUDFLARE_API_TOKEN=<scoped token>
#      Token scopes: Account > Cloudflare Pages: Edit, Account > D1: Edit,
#                    Zone > DNS: Edit (zone jagular.io)  [for the custom domain]
#   And choose the stats password + salt:
#      export STATS_TOKEN=<a password you pick>
#      export IP_SALT=<any random string>
set -euo pipefail
cd "$(dirname "$0")"

PROJECT=ai-map
DB=ai-map-visits
DOMAIN=ai-news.jagular.io
ACCOUNT_ID=4d92d2ca307430c0f6706269f613fb47
WR="npx --yes wrangler"

echo "== 0. install wrangler =="
npm install

echo "== 1. create the Pages project (direct upload) =="
$WR pages project create "$PROJECT" --production-branch=main || echo "  (project may already exist — ok)"

echo "== 2. create the D1 database and wire its id into wrangler.toml =="
OUT="$($WR d1 create "$DB" 2>&1 || true)"; echo "$OUT"
ID="$(printf '%s' "$OUT" | grep -oiE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1 || true)"
if [ -z "$ID" ]; then
  ID="$($WR d1 list --json 2>/dev/null | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{const a=JSON.parse(s);const m=a.find(x=>x.name==="'"$DB"'");if(m)process.stdout.write(m.uuid||m.id||"")}catch(e){}})' || true)"
fi
if [ -n "$ID" ]; then
  sed -i "s/database_id = \".*\"/database_id = \"$ID\"/" wrangler.toml
  echo "  database_id = $ID  -> written to wrangler.toml"
else
  echo "  !! could not detect database_id automatically; paste it into wrangler.toml before continuing" >&2
fi

echo "== 3. apply the schema to the remote D1 =="
$WR d1 execute "$DB" --remote --file=schema.sql

echo "== 4. set secrets (stats password + ip salt) =="
: "${STATS_TOKEN:?set STATS_TOKEN to the password you want for /stats.html}"
printf '%s' "$STATS_TOKEN" | $WR pages secret put STATS_TOKEN --project-name="$PROJECT"
printf '%s' "${IP_SALT:-ai-map-$(head -c8 /dev/urandom | od -An -tx1 | tr -d ' \n')}" | $WR pages secret put IP_SALT --project-name="$PROJECT"

echo "== 5. first deploy =="
./build-deploy.sh

echo "== 6. attach the custom domain ($DOMAIN) =="
# CLI (newer wrangler):
if ! $WR pages domain add "$PROJECT" "$DOMAIN" 2>/dev/null; then
  echo "  CLI domain add unavailable; using the API instead"
  curl -fsS -X POST \
    "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID/pages/projects/$PROJECT/domains" \
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN:?need token for the API call}" \
    -H "Content-Type: application/json" \
    --data "{\"name\":\"$DOMAIN\"}" && echo
fi

cat <<EOF

== DONE ==
Production URL : https://$PROJECT.pages.dev
Custom domain  : https://$DOMAIN   (DNS record is created/updated by Cloudflare; give it a few minutes + cert issuance)
Private stats  : https://$DOMAIN/stats.html   (key = your STATS_TOKEN)

Next:
  * The old cloudflared tunnel for $DOMAIN is now bypassed (DNS points at Pages).
    You can stop it when you're happy:  systemctl disable --now cloudflared
  * Wire the hourly refresh to redeploy (see README.md).
EOF
