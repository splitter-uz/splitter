#!/usr/bin/env bash
###############################################################################
# Splitter — bare-metal installer for a Linux host.
#
# Installs dependencies (nginx + stream module, python3/venv, iproute2,
# dhcp client, openssl), wires the nginx `stream {}` include, builds the venv,
# detects the default network interface, and (optionally) installs a systemd
# service. Idempotent — safe to re-run.
#
# Usage:
#   sudo ./setup.sh                 # install deps + configure, print run cmd
#   sudo ./setup.sh --service       # also install + start the systemd service
#   sudo ./setup.sh --nic ens3      # force the parent interface
#   sudo ./setup.sh --no-deps       # skip package installation
#   sudo ./setup.sh --port 8088 --host 0.0.0.0   # 0.0.0.0 = all interfaces
#   sudo ./setup.sh --waf           # also install the WAF now (by default it is
#                                   # NOT installed here — do it from the
#                                   # dashboard's WAF page instead)
#   sudo ./setup.sh --waf-enforce   # flip the WAF from Detection to blocking mode
#   sudo ./setup.sh --waf-only      # (re)install ONLY the WAF (used by the UI's
#                                   # Install/Repair button; skips deps/service)
###############################################################################
set -euo pipefail

# ---------------------------------------------------------------------------
INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"
STREAM_DIR="/etc/nginx/stream.d"
SSL_DIR="/etc/nginx/ssl"
ACL_DIR="/etc/nginx/acl.d"
NGINX_CONF="/etc/nginx/nginx.conf"
MODSEC_DIR="/etc/nginx/modsec"           # WAF: ModSecurity config + CRS
CONFD="/etc/nginx/conf.d"                # WAF: server block lives here
WAF_MODULE_SO="/usr/lib/nginx/modules/ngx_http_modsecurity_module.so"

NIC=""
DO_SERVICE=0
DO_DEPS=1
DO_WAF=0          # WAF is installed from the dashboard (WAF page), not by setup
DO_WAF_ENFORCE=0  # --waf-enforce: just flip Detection -> blocking and exit
WAF_ONLY=0        # --waf-only: (re)install just the WAF, skip everything else
HOST_SET=0
UI_HOST="0.0.0.0"
UI_PORT="8088"

c_g(){ printf '\033[32m%s\033[0m\n' "$*"; }
c_y(){ printf '\033[33m%s\033[0m\n' "$*"; }
c_r(){ printf '\033[31m%s\033[0m\n' "$*"; }
step(){ printf '\n\033[36m==> %s\033[0m\n' "$*"; }
die(){ c_r "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --service)  DO_SERVICE=1 ;;
    --no-deps)  DO_DEPS=0 ;;
    --waf)      DO_WAF=1 ;;
    --no-waf)   DO_WAF=0 ;;
    --waf-enforce) DO_WAF_ENFORCE=1 ;;
    --waf-only) WAF_ONLY=1; DO_WAF=1 ;;
    --nic)      NIC="${2:?--nic needs a value}"; shift ;;
    --host)     UI_HOST="${2:?--host needs a value}"; HOST_SET=1; shift ;;
    --port)     UI_PORT="${2:?--port needs a value}"; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)          die "unknown argument: $1" ;;
  esac
  shift
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[ "$(uname -s)" = "Linux" ] || die "Splitter provisions Linux interfaces; run this on a Linux host."
[ "$(id -u)" = "0" ] || die "Run as root (sudo ./setup.sh)."

PM=""
for c in apt-get dnf yum pacman zypper; do
  command -v "$c" >/dev/null 2>&1 && { PM="$c"; break; }
done

# --waf-enforce: flip the WAF from Detection to blocking mode, then exit. This
# is the one WAF action you run on its own (weeks after install), so it short-
# circuits the full setup.
if [ "$DO_WAF_ENFORCE" = "1" ]; then
  step "Switching ModSecurity to blocking mode (SecRuleEngine On)"
  [ -f "$MODSEC_DIR/modsecurity.conf" ] || die "WAF not installed yet — install it from the dashboard first."
  sed -i 's/^SecRuleEngine .*/SecRuleEngine On/' "$MODSEC_DIR/modsecurity.conf"
  nginx -t || die "nginx -t failed — not reloading. Fix the config and retry."
  systemctl reload nginx 2>/dev/null || nginx -s reload
  c_g "ModSecurity is now ENFORCING. Watch /var/log/modsec_audit.log for blocks."
  exit 0
fi

# The WAF server block lives in conf.d, so a BROKEN one fails every `nginx -t`
# — including the base setup below and the app's own L4-proxy reloads. Since the
# WAF is managed from the dashboard (not by setup), drop a broken block here so
# setup always proceeds; reinstall it from the WAF page afterwards. (--waf-only
# rebuilds it right after, so this is safe there too.)
if command -v nginx >/dev/null 2>&1 && [ -f "$CONFD/splitter-waf.conf" ] \
   && ! nginx -t >/dev/null 2>&1; then
  c_y "Existing WAF config fails 'nginx -t' — removing it so nginx is valid."
  c_y "Reinstall the WAF from the dashboard (WAF page -> Install) when ready."
  rm -f "$CONFD/splitter-waf.conf" "$CONFD/splitter-waf-proxy.inc"
fi

# Everything from here to the WAF section is the full host setup. --waf-only
# skips all of it and jumps straight to (re)installing the WAF.
if [ "$WAF_ONLY" = "0" ]; then

# ---------------------------------------------------------------------------
# 1. Dependencies
# ---------------------------------------------------------------------------
install_deps() {
  step "Installing dependencies via $PM"
  case "$PM" in
    apt-get)
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y --no-install-recommends \
        nginx libnginx-mod-stream python3 python3-venv \
        iproute2 isc-dhcp-client openssl iptables \
        iputils-ping traceroute tcpdump whois dnsutils net-tools
      ;;
    dnf|yum)
      "$PM" install -y nginx python3 iproute openssl iptables || true
      "$PM" install -y nginx-mod-stream || c_y "nginx-mod-stream not found — your nginx may already include stream."
      "$PM" install -y dhcp-client || "$PM" install -y dhclient || c_y "no dhcp client (DHCP allocation will be unavailable)."
      "$PM" install -y iputils traceroute tcpdump whois bind-utils net-tools || \
        c_y "Some diagnostic tools could not be installed — Tools page features may be limited."
      ;;
    pacman)
      pacman -Sy --noconfirm nginx-mainline python iproute2 openssl || \
        pacman -Sy --noconfirm nginx python iproute2 openssl
      pacman -Sy --noconfirm dhclient || c_y "no dhcp client (DHCP allocation unavailable)."
      pacman -Sy --noconfirm iptables-nft || pacman -Sy --noconfirm iptables || \
        c_y "iptables not installed — the Firewall page will be unavailable."
      pacman -Sy --noconfirm iputils traceroute tcpdump whois bind-tools net-tools || \
        c_y "Some diagnostic tools could not be installed — Tools page features may be limited."
      ;;
    zypper)
      zypper --non-interactive install nginx python3 iproute2 openssl dhcp-client iptables || true
      zypper --non-interactive install iputils traceroute tcpdump whois bind-utils net-tools || \
        c_y "Some diagnostic tools could not be installed — Tools page features may be limited."
      ;;
    "")
      c_y "No known package manager detected — install manually: nginx(+stream), python3+venv, iproute2, a dhcp client, openssl, iptables."
      c_y "Also install for the Tools page: iputils, traceroute, tcpdump, whois, bind-utils (or dnsutils), net-tools."
      ;;
  esac
}

check_tools() {
  step "Checking diagnostic tool availability"
  local missing=()
  for cmd in ping traceroute tcpdump whois dig netstat; do
    command -v "$cmd" >/dev/null 2>&1 && c_g "  [ok] $cmd" || missing+=("$cmd")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    c_y "  [missing] ${missing[*]}"
    c_y "  The Tools page will show errors for commands that are not installed."
  fi
  command -v iptables >/dev/null 2>&1 && c_g "  [ok] iptables" || \
    c_y "  [missing] iptables — the Firewall page will be unavailable."
}
[ "$DO_DEPS" = "1" ] && install_deps || c_y "Skipping dependency install (--no-deps)."

command -v nginx >/dev/null 2>&1 || die "nginx is not installed."
command -v python3 >/dev/null 2>&1 || die "python3 is not installed."
command -v ip >/dev/null 2>&1 || die "iproute2 (ip) is not installed."
check_tools

# ---------------------------------------------------------------------------
# 2. nginx: stream.d + ssl dirs and the stream {} include
# ---------------------------------------------------------------------------
step "Configuring nginx stream context"
mkdir -p "$STREAM_DIR" "$SSL_DIR" "$ACL_DIR"
chmod 755 "$STREAM_DIR" "$ACL_DIR"; chmod 700 "$SSL_DIR"

# If we're going to (re)install the WAF, clear any existing WAF server block
# first so a stale/broken one can't fail the base `nginx -t` below (and abort
# setup before install_waf gets a chance to rebuild it). install_waf recreates
# it cleanly at the end.
if [ "$DO_WAF" = "1" ]; then
  rm -f "$CONFD/splitter-waf.conf" "$CONFD/splitter-waf-proxy.inc"
fi

if grep -Rqs "stream.d/\*.conf" "$NGINX_CONF" /etc/nginx/conf.d 2>/dev/null; then
  c_g "stream include already present."
elif grep -qE '^\s*stream\s*\{' "$NGINX_CONF"; then
  c_y "An existing 'stream {}' block was found in $NGINX_CONF."
  c_y "Add this line inside it manually:  include $STREAM_DIR/*.conf;"
else
  cat >> "$NGINX_CONF" <<EOF

# >>> splitter >>>  (managed by setup.sh)
stream {
    include $STREAM_DIR/*.conf;
}
# <<< splitter <<<
EOF
  c_g "Added top-level stream {} block including $STREAM_DIR/*.conf"
fi

step "Validating nginx configuration"
if ! nginx -t 2>/tmp/splitter_nginxt; then
  cat /tmp/splitter_nginxt
  if grep -qi 'unknown directive "stream"' /tmp/splitter_nginxt; then
    die "nginx lacks the stream module. Install it (e.g. apt: libnginx-mod-stream, dnf: nginx-mod-stream) and re-run."
  fi
  die "nginx -t failed (see above)."
fi
c_g "nginx -t OK"
# Make sure nginx is enabled and running so later reloads (on Apply) work.
systemctl enable --now nginx 2>/dev/null || true
systemctl reload nginx 2>/dev/null || nginx -s reload 2>/dev/null || true

# ---------------------------------------------------------------------------
# 3. Persistent data directory (survives redeploys — never put this in the repo)
# ---------------------------------------------------------------------------
step "Preparing persistent data directory"
DATA_DIR="/var/lib/splitter"
mkdir -p "$DATA_DIR"; chmod 700 "$DATA_DIR"
# One-time migration: if an old in-repo store exists and the new one is empty,
# move the mappings so a redeploy doesn't appear to "lose" them.
if [ -f "$INSTALL_DIR/backend/data/mappings.json" ] && [ ! -f "$DATA_DIR/mappings.json" ]; then
  cp -a "$INSTALL_DIR/backend/data/." "$DATA_DIR/" 2>/dev/null || true
  c_y "Migrated existing mappings from backend/data -> $DATA_DIR"
fi
c_g "data dir: $DATA_DIR"

# ---------------------------------------------------------------------------
# 3b. Kernel: let nginx bind IPs that aren't fully up yet
# ---------------------------------------------------------------------------
# A reload/restart fired right after `ip addr add` (and nginx's own start at
# boot, before Splitter re-adds the IPs) otherwise fails with
#   bind() to <ip>:443 failed (99: Cannot assign requested address)
# even though a manual restart later works once the address settles.
# net.ipv4.ip_nonlocal_bind=1 lets nginx bind a not-yet-local address, so every
# (re)start behaves like that "fast" manual one.
step "Configuring kernel (ip_nonlocal_bind) for nginx"
SYSCTL_FILE=/etc/sysctl.d/99-splitter.conf
cat > "$SYSCTL_FILE" <<'EOS'
# Installed by Splitter — allow nginx to bind bind-IPs that are not (yet) up.
net.ipv4.ip_nonlocal_bind = 1
net.ipv6.ip_nonlocal_bind = 1
EOS
# Load it now (systemd applies /etc/sysctl.d early at boot, before nginx).
sysctl -p "$SYSCTL_FILE" >/dev/null 2>&1 || sysctl --system >/dev/null 2>&1 || true
if [ "$(sysctl -n net.ipv4.ip_nonlocal_bind 2>/dev/null)" = "1" ]; then
  c_g "ip_nonlocal_bind active now and persisted in $SYSCTL_FILE"
else
  c_y "WARNING: could not set net.ipv4.ip_nonlocal_bind (need root?). nginx may"
  c_y "         fail to bind freshly-added IPs until: sudo sysctl -p $SYSCTL_FILE"
fi

# Order nginx after the network is up and sysctl has applied, so its boot-time
# bind of existing mapping IPs sees ip_nonlocal_bind=1. (No Restart= here: the
# app drives nginx with `nginx -s stop`/start, and a systemd auto-restart would
# race it.)
NGINX_DROPIN=/etc/systemd/system/nginx.service.d
mkdir -p "$NGINX_DROPIN"
cat > "$NGINX_DROPIN/splitter.conf" <<'EOS'
# Installed by Splitter.
[Unit]
After=network-online.target systemd-sysctl.service
Wants=network-online.target
EOS
systemctl daemon-reload 2>/dev/null || true
c_g "nginx ordered after network-online + sysctl"

# ---------------------------------------------------------------------------
# 4. Python venv
# ---------------------------------------------------------------------------
step "Setting up Python virtualenv"
cd "$INSTALL_DIR"
[ -d .venv ] || python3 -m venv .venv
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r backend/requirements.txt
c_g "venv ready at $INSTALL_DIR/.venv"

# ---------------------------------------------------------------------------
# 5. Detect default interface
# ---------------------------------------------------------------------------
if [ -z "$NIC" ]; then
  NIC="$(ip -o -4 route show to default 2>/dev/null | awk '{print $5; exit}')"
  [ -n "$NIC" ] || NIC="$(ip -o link show up 2>/dev/null | awk -F': ' '$2!="lo"{print $2; exit}')"
fi
[ -n "$NIC" ] || NIC="eth0"
c_g "Parent interface: $NIC"

# ---------------------------------------------------------------------------
# 5. systemd service (optional)
# ---------------------------------------------------------------------------
install_service() {
  step "Installing systemd service"
  local unit=/etc/systemd/system/splitter.service
  cat > "$unit" <<EOF
[Unit]
Description=Splitter - L4 Stream Proxy Manager
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment=SPLITTER_SIMULATE=0
Environment=SPLITTER_DATA_DIR=$DATA_DIR
Environment=SPLITTER_SUDO=
Environment=SPLITTER_NIC=$NIC
Environment=SPLITTER_BIND_PREFIX=24
Environment=SPLITTER_HOST=$UI_HOST
Environment=SPLITTER_PORT=$UI_PORT
# Reload/restart nginx via the nginx binary (not systemctl) so freshly-added
# bind IPs are picked up reliably.
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/backend/app.py
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now splitter
  sleep 1
  systemctl --no-pager --lines=0 status splitter || true
  c_g "Service installed. Logs: journalctl -u splitter -f"
}

fi   # end full-setup sections (skipped by --waf-only)

# ---------------------------------------------------------------------------
# 6. WAF — ModSecurity + OWASP CRS reverse proxy in front of the app (default)
# ---------------------------------------------------------------------------
# Self-contained: installs the engine + connector (building it from source when
# the distro doesn't package it), the OWASP CRS, and renders the nginx server
# block, ModSecurity config and a self-signed cert. Starts in DetectionOnly so
# it can never lock you out. Returns non-zero (without aborting setup) on any
# failure, so the app is simply left on its normal host.
install_waf() {
  step "Installing ModSecurity + OWASP CRS WAF"
  command -v nginx >/dev/null 2>&1 || { c_r "  nginx not installed — run full setup first"; return 1; }
  mkdir -p "$MODSEC_DIR" "$SSL_DIR" "$CONFD"

  # --- engine + nginx connector -------------------------------------------
  if [ "$PM" = "apt-get" ]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y --no-install-recommends libmodsecurity3 >/dev/null 2>&1 || true
    if apt-get install -y --no-install-recommends libnginx-mod-http-modsecurity >/dev/null 2>&1; then
      WAF_MODULE_SO=""   # distro package self-registers via modules-enabled
      c_g "  connector installed from package"
    fi
  else
    c_y "  non-apt distro: will try to build the connector; install libmodsecurity + modsecurity-nginx manually if it fails"
  fi

  # Build the connector as a dynamic module matching this nginx, if unpackaged.
  if [ -n "$WAF_MODULE_SO" ] && [ ! -f "$WAF_MODULE_SO" ]; then
    c_y "  connector not packaged — building it for your nginx (~2-4 min)"
    if [ "$PM" = "apt-get" ]; then
      apt-get install -y --no-install-recommends git g++ make automake libtool \
        pkg-config libmodsecurity-dev libpcre3-dev zlib1g-dev libssl-dev wget \
        >/dev/null 2>&1 || { c_r "  could not install build deps"; return 1; }
    elif ! command -v git >/dev/null 2>&1; then
      c_r "  need git + build tools + libmodsecurity-dev to build the connector"; return 1
    fi
    local ngver build
    ngver="$(nginx -v 2>&1 | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')"
    [ -n "$ngver" ] || { c_r "  could not read nginx version"; return 1; }
    build="$(mktemp -d)"
    if ( cd "$build" \
        && git clone -q --depth 1 https://github.com/owasp-modsecurity/ModSecurity-nginx.git \
        && wget -q "https://nginx.org/download/nginx-${ngver}.tar.gz" \
        && tar -xzf "nginx-${ngver}.tar.gz" && cd "nginx-${ngver}" \
        && eval ./configure --with-compat \
             --add-dynamic-module="$build/ModSecurity-nginx" \
             $(nginx -V 2>&1 | sed -n 's/^configure arguments: //p') >/dev/null 2>&1 \
        && make modules >/dev/null 2>&1 \
        && mkdir -p /usr/lib/nginx/modules \
        && cp objs/ngx_http_modsecurity_module.so "$WAF_MODULE_SO" ); then
      c_g "  built $WAF_MODULE_SO for nginx $ngver"
    else
      c_r "  connector build failed"; rm -rf "$build"; return 1
    fi
    rm -rf "$build"
  fi

  # Load the built module at the top of nginx.conf (main context).
  if [ -n "$WAF_MODULE_SO" ] && [ -f "$WAF_MODULE_SO" ]; then
    grep -q "ngx_http_modsecurity_module.so" "$NGINX_CONF" || \
      sed -i "1i load_module modules/ngx_http_modsecurity_module.so;" "$NGINX_CONF"
  fi

  # --- OWASP Core Rule Set -------------------------------------------------
  if [ ! -f "$MODSEC_DIR/crs/crs-setup.conf" ]; then
    command -v git >/dev/null 2>&1 || \
      { [ "$PM" = "apt-get" ] && apt-get install -y --no-install-recommends git >/dev/null 2>&1; }
    git clone -q --depth 1 https://github.com/coreruleset/coreruleset.git "$MODSEC_DIR/crs" \
      || { c_r "  CRS clone failed (need git + network)"; return 1; }
    cp "$MODSEC_DIR/crs/crs-setup.conf.example" "$MODSEC_DIR/crs/crs-setup.conf"
  fi
  # Some CRS versions define their own SecDefaultAction in crs-setup.conf, which
  # collides with the one in our base config (libmodsecurity allows only one per
  # phase). Strip it so our base config is the single source. Run every install
  # so an existing crs-setup.conf is repaired too.
  [ -f "$MODSEC_DIR/crs/crs-setup.conf" ] && \
    sed -i '/^[[:space:]]*SecDefaultAction/d' "$MODSEC_DIR/crs/crs-setup.conf"

  # --- ModSecurity base config (DetectionOnly first) ----------------------
  # Always (re)write so re-running setup repairs a broken/duplicated file.
  # Prefer the distro's recommended config (known-good for its libmodsecurity);
  # otherwise a minimal fallback.
  local rec=""
  for p in /etc/nginx/modsecurity/modsecurity.conf-recommended \
           /etc/modsecurity/modsecurity.conf-recommended \
           /usr/share/modsecurity*/modsecurity.conf-recommended; do
    [ -f "$p" ] && rec="$p" && break
  done
  if [ -n "$rec" ]; then
    cp "$rec" "$MODSEC_DIR/modsecurity.conf"
    c_g "  base config from $rec"
  else
    cat > "$MODSEC_DIR/modsecurity.conf" <<'EOF'
SecRuleEngine DetectionOnly
SecRequestBodyAccess On
SecResponseBodyAccess Off
SecAuditEngine RelevantOnly
SecAuditLog /var/log/modsec_audit.log
SecAuditLogType Serial
SecDefaultAction "phase:1,log,auditlog,pass"
SecDefaultAction "phase:2,log,auditlog,pass"
EOF
  fi
  # Force DetectionOnly and collapse to EXACTLY ONE SecDefaultAction. Some
  # libmodsecurity builds reject a *second* SecDefaultAction even for a different
  # phase ("SecDefaultActions can only be placed once per phase ... Phase 1 was
  # informed already") — the nginx -t failure seen here. Strip them all and add a
  # single phase:2 default; CRS still blocks via its phase:2 anomaly-evaluation
  # rules (explicit deny), so one default is sufficient.
  sed -i 's/^SecRuleEngine .*/SecRuleEngine DetectionOnly/' "$MODSEC_DIR/modsecurity.conf"
  sed -i '/^[[:space:]]*SecDefaultAction/d' "$MODSEC_DIR/modsecurity.conf"
  printf 'SecDefaultAction "phase:2,log,auditlog,pass"\n' >> "$MODSEC_DIR/modsecurity.conf"

  # Master include: base + CRS + Splitter tuning (exclusions for admin payloads
  # the generic CRS rules would otherwise flag: cert uploads, config previews,
  # host/DNS files, CIDR lists, diagnostic-tool args).
  cat > "$MODSEC_DIR/main.conf" <<'EOF'
Include /etc/nginx/modsec/modsecurity.conf
Include /etc/nginx/modsec/crs/crs-setup.conf
Include /etc/nginx/modsec/crs/rules/*.conf

# --- Splitter tuning (must come AFTER the CRS includes) ---
SecRule REQUEST_URI "@beginsWith /api/ssl/certs" \
    "id:190001,phase:1,pass,nolog,ctl:ruleRemoveByTag=attack-sqli,ctl:ruleRemoveByTag=attack-rce,ctl:ruleRemoveByTag=attack-injection-php"
SecRule REQUEST_URI "@rx ^/api/(preview|import|reapply)\b" \
    "id:190002,phase:1,pass,nolog,ctl:ruleRemoveByTag=attack-injection-generic,ctl:ruleRemoveByTag=attack-rce,ctl:ruleRemoveByTag=attack-lfi"
SecRule REQUEST_URI "@rx ^/api/network/(hosts|dns)\b" \
    "id:190003,phase:1,pass,nolog,ctl:ruleRemoveByTag=attack-injection-generic"
SecRule REQUEST_URI "@beginsWith /api/tools/" \
    "id:190004,phase:1,pass,nolog,ctl:ruleRemoveByTag=attack-rce,ctl:ruleRemoveByTag=attack-injection-generic"
SecRule REQUEST_URI "@rx ^/api/(access-lists|mappings)\b" \
    "id:190005,phase:1,pass,nolog,ctl:ruleRemoveByTag=attack-injection-generic"
EOF

  # --- nginx server block + shared proxy include --------------------------
  # (The WAF page in the dashboard re-renders this file when you change the
  # port or rate limits.)
  cat > "$CONFD/splitter-waf.conf" <<EOF
# Splitter WAF — HTTP reverse proxy + ModSecurity in front of the admin UI.
limit_req_zone \$binary_remote_addr zone=splitter_login:10m rate=10r/m;
limit_req_zone \$binary_remote_addr zone=splitter_api:10m rate=30r/s;

server {
    listen 8443 ssl;
    listen [::]:8443 ssl;
    server_name _;

    ssl_certificate     $SSL_DIR/splitter-waf.crt;
    ssl_certificate_key $SSL_DIR/splitter-waf.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    modsecurity on;
    modsecurity_rules_file $MODSEC_DIR/main.conf;

    add_header X-Frame-Options           "DENY"        always;
    add_header X-Content-Type-Options    "nosniff"     always;
    add_header Referrer-Policy           "same-origin" always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 2m;

    location = /api/login {
        limit_req zone=splitter_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:$UI_PORT;
        include $CONFD/splitter-waf-proxy.inc;
    }
    location = /api/setup {
        limit_req zone=splitter_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:$UI_PORT;
        include $CONFD/splitter-waf-proxy.inc;
    }
    location / {
        limit_req zone=splitter_api burst=60 nodelay;
        limit_req_status 429;
        proxy_pass http://127.0.0.1:$UI_PORT;
        include $CONFD/splitter-waf-proxy.inc;
    }
}
EOF
  cat > "$CONFD/splitter-waf-proxy.inc" <<'EOF'
proxy_http_version 1.1;
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_read_timeout 120s;
EOF

  # --- self-signed cert (swap for a real one at $SSL_DIR/splitter-waf.*) ---
  if [ ! -f "$SSL_DIR/splitter-waf.crt" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
      -keyout "$SSL_DIR/splitter-waf.key" -out "$SSL_DIR/splitter-waf.crt" \
      -subj "/CN=splitter-waf" >/dev/null 2>&1
    chmod 600 "$SSL_DIR/splitter-waf.key"
  fi

  # --- validate + reload ---------------------------------------------------
  # If validation fails, ROLL BACK our conf.d files so nginx stays valid — a
  # broken block here would otherwise fail every future `nginx -t` and block the
  # app from reloading its own L4 proxy config.
  if ! nginx -t 2>/tmp/splitter_waf_nginxt; then
    cat /tmp/splitter_waf_nginxt
    c_r "  nginx -t failed after WAF install — rolling back the WAF config so nginx stays valid"
    rm -f "$CONFD/splitter-waf.conf" "$CONFD/splitter-waf-proxy.inc"
    return 1
  fi
  systemctl reload nginx 2>/dev/null || nginx -s reload
  c_g "  WAF installed (DetectionOnly) — fronting the app on https://<host>:8443"
  return 0
}

if [ "$DO_WAF" = "1" ]; then
  if install_waf; then
    # WAF is up — the app should only be reachable THROUGH it. Bind to loopback
    # unless the operator explicitly chose a host. (On failure we leave the app
    # on its normal host so a WAF build problem can't lock you out.)
    if [ "$HOST_SET" = "0" ]; then
      UI_HOST="127.0.0.1"
      c_g "App will bind 127.0.0.1 (reachable only via the WAF on :8443)."
    fi
  else
    c_y "WAF install did not complete — leaving the app on $UI_HOST (not locking you out)."
    c_y "Fix the cause and retry from the dashboard's WAF page, or run: sudo ./setup.sh --waf-only"
    [ "$WAF_ONLY" = "1" ] && exit 1
  fi
fi

# --waf-only: the WAF is (re)installed — stop before touching deps/service. The
# app is already running (this path is used by the dashboard's Install button).
if [ "$WAF_ONLY" = "1" ]; then
  c_g "WAF-only (re)install complete."
  exit 0
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
if [ "$DO_SERVICE" = "1" ]; then
  install_service
  echo
  c_g "Splitter is running -> http://$UI_HOST:$UI_PORT"
  if [ "$DO_WAF" = "1" ]; then
    c_g "WAF (DetectionOnly) fronting it -> https://<host>:8443  (enforce: sudo ./setup.sh --waf-enforce)"
  else
    c_g "WAF not installed — enable it anytime from the dashboard: WAF page -> Install."
  fi
else
  echo
  c_g "Setup complete."
  echo "Start it now (LIVE provisioning needs root):"
  echo "    sudo SPLITTER_NIC=$NIC SPLITTER_SUDO= SPLITTER_DATA_DIR=$DATA_DIR $INSTALL_DIR/.venv/bin/python $INSTALL_DIR/backend/app.py"
  echo "Or install the background service:"
  echo "    sudo ./setup.sh --service"
  echo
  echo "Then open:  http://$UI_HOST:$UI_PORT"
fi
