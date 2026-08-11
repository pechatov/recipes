#!/usr/bin/env bash
set -Eeuo pipefail

: "${NPM_DATA_DIR:?Set NPM_DATA_DIR}"
: "${RECIPES_CERTIFICATE_ID:?Set RECIPES_CERTIFICATE_ID}"

RECIPES_PRIMARY_DOMAIN="${RECIPES_PRIMARY_DOMAIN:-recipes.pechatov.com}"
RECIPES_ALIAS_DOMAIN="${RECIPES_ALIAS_DOMAIN:-gotovka.pechatov.com}"
RECIPES_FORWARD_HOST="${RECIPES_FORWARD_HOST:-192.168.31.2}"
RECIPES_FORWARD_PORT="${RECIPES_FORWARD_PORT:-30111}"
NPM_CONTAINER="${NPM_CONTAINER:-ix-nginx-proxy-manager-npm-1}"
EXPECTED_DATA_DIR="/mnt/main-pool/config/nginx/data"

if [[ "$NPM_DATA_DIR" != "$EXPECTED_DATA_DIR" ]]; then
  echo "Refusing to modify unexpected NPM data directory: $NPM_DATA_DIR" >&2
  exit 1
fi
for domain in "$RECIPES_PRIMARY_DOMAIN" "$RECIPES_ALIAS_DOMAIN"; do
  if [[ ! "$domain" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]]; then
    echo "Invalid domain: $domain" >&2
    exit 1
  fi
done
if [[ "$RECIPES_PRIMARY_DOMAIN" == "$RECIPES_ALIAS_DOMAIN" ]]; then
  echo "Primary and alias domains must differ." >&2
  exit 1
fi
if [[ ! "$RECIPES_FORWARD_HOST" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "Invalid forward host." >&2
  exit 1
fi
if [[ ! "$RECIPES_FORWARD_PORT" =~ ^[0-9]+$ ]] || (( RECIPES_FORWARD_PORT < 1 || RECIPES_FORWARD_PORT > 65535 )); then
  echo "Invalid forward port." >&2
  exit 1
fi
if [[ ! "$RECIPES_CERTIFICATE_ID" =~ ^[1-9][0-9]*$ ]]; then
  echo "Invalid certificate ID." >&2
  exit 1
fi
if [[ ! "$NPM_CONTAINER" =~ ^[A-Za-z0-9_.-]+$ ]]; then
  echo "Invalid NPM container name." >&2
  exit 1
fi

db_path="$NPM_DATA_DIR/database.sqlite"
proxy_dir="$NPM_DATA_DIR/nginx/proxy_host"
backup_dir="$NPM_DATA_DIR/backups"
test -f "$db_path"
install -d -m 0750 "$proxy_dir" "$backup_dir"

certificate_exists="$(
  sqlite3 "$db_path" \
    "select count(*) from certificate where id = $RECIPES_CERTIFICATE_ID and is_deleted = 0;"
)"
if [[ "$certificate_exists" != "1" ]]; then
  echo "NPM certificate $RECIPES_CERTIFICATE_ID does not exist." >&2
  exit 1
fi

mapfile -t existing_ids < <(
  sqlite3 "$db_path" \
    "select distinct p.id from proxy_host p, json_each(p.domain_names) d where p.is_deleted = 0 and d.value in ('$RECIPES_PRIMARY_DOMAIN', '$RECIPES_ALIAS_DOMAIN') order by p.id;"
)
if (( ${#existing_ids[@]} > 1 )); then
  echo "Domains belong to different proxy hosts; refusing an ambiguous update." >&2
  exit 1
fi

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
database_backup="$backup_dir/database.sqlite.before-recipes-$timestamp"
sqlite3 "$db_path" ".backup '$database_backup'"

domain_names="[\"$RECIPES_PRIMARY_DOMAIN\",\"$RECIPES_ALIAS_DOMAIN\"]"
if (( ${#existing_ids[@]} == 1 )); then
  proxy_id="${existing_ids[0]}"
  if [[ -f "$proxy_dir/$proxy_id.conf" ]]; then
    cp -a "$proxy_dir/$proxy_id.conf" "$backup_dir/proxy-host-$proxy_id.conf.before-recipes-$timestamp"
  fi
  sqlite3 "$db_path" <<SQL
update proxy_host
set modified_on = datetime('now'),
    domain_names = '$domain_names',
    forward_host = '$RECIPES_FORWARD_HOST',
    forward_port = $RECIPES_FORWARD_PORT,
    certificate_id = $RECIPES_CERTIFICATE_ID,
    ssl_forced = 1,
    caching_enabled = 0,
    block_exploits = 1,
    allow_websocket_upgrade = 1,
    http2_support = 1,
    forward_scheme = 'http',
    enabled = 1,
    locations = '[]',
    hsts_enabled = 1,
    hsts_subdomains = 0,
    trust_forwarded_proto = 0,
    meta = '{"nginx_online":true,"nginx_err":null}'
where id = $proxy_id;
SQL
else
  proxy_id="$(sqlite3 "$db_path" 'select coalesce(max(id), 0) + 1 from proxy_host;')"
  sqlite3 "$db_path" <<SQL
insert into proxy_host (
  id, created_on, modified_on, owner_user_id, is_deleted, domain_names,
  forward_host, forward_port, access_list_id, certificate_id, ssl_forced,
  caching_enabled, block_exploits, advanced_config, meta,
  allow_websocket_upgrade, http2_support, forward_scheme, enabled, locations,
  hsts_enabled, hsts_subdomains, trust_forwarded_proto
) values (
  $proxy_id, datetime('now'), datetime('now'), 1, 0, '$domain_names',
  '$RECIPES_FORWARD_HOST', $RECIPES_FORWARD_PORT, 0, $RECIPES_CERTIFICATE_ID, 1,
  0, 1, '', '{"nginx_online":true,"nginx_err":null}',
  1, 1, 'http', 1, '[]', 1, 0, 0
);
SQL
fi

cat >"$proxy_dir/$proxy_id.conf" <<CONF
# ------------------------------------------------------------
# $RECIPES_PRIMARY_DOMAIN, $RECIPES_ALIAS_DOMAIN
# ------------------------------------------------------------

map \$scheme \$hsts_header {
  https "max-age=31536000";
}

server {
  set \$forward_scheme http;
  set \$server "$RECIPES_FORWARD_HOST";
  set \$port $RECIPES_FORWARD_PORT;

  listen 80;
  listen [::]:80;
  listen 443 ssl;
  listen [::]:443 ssl;

  server_name $RECIPES_PRIMARY_DOMAIN $RECIPES_ALIAS_DOMAIN;
  http2 on;

  include conf.d/include/letsencrypt-acme-challenge.conf;
  include conf.d/include/ssl-cache.conf;
  include conf.d/include/ssl-ciphers.conf;
  ssl_certificate /etc/letsencrypt/live/npm-$RECIPES_CERTIFICATE_ID/fullchain.pem;
  ssl_certificate_key /etc/letsencrypt/live/npm-$RECIPES_CERTIFICATE_ID/privkey.pem;

  include conf.d/include/block-exploits.conf;
  set \$trust_forwarded_proto "F";
  include conf.d/include/force-ssl.conf;
  add_header Strict-Transport-Security \$hsts_header always;

  client_max_body_size 16m;
  proxy_set_header Upgrade \$http_upgrade;
  proxy_set_header Connection \$http_connection;
  proxy_http_version 1.1;

  access_log /data/logs/proxy-host-${proxy_id}_access.log proxy;
  error_log /data/logs/proxy-host-${proxy_id}_error.log warn;

  location / {
    add_header Strict-Transport-Security \$hsts_header always;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$http_connection;
    proxy_http_version 1.1;
    include conf.d/include/proxy.conf;
  }

  include /data/nginx/custom/server_proxy[.]conf;
}
CONF

docker exec "$NPM_CONTAINER" nginx -t
docker exec "$NPM_CONTAINER" nginx -s reload

echo "Configured Nginx Proxy Manager host $proxy_id for $domain_names."
echo "Database backup: $database_backup"
