# Splitter, containerized.
#
# This image bundles nginx (with the stream module) and the Splitter Flask app.
# It is meant to run in the HOST network namespace with kernel privileges
# (see docker-compose.yml), so the macvlan/VLAN interfaces and IPs it creates
# are REAL and reachable on the LAN — exactly like running setup.sh on the host.

# --- Frontend CSS build stage ----------------------------------------------
# Compiles a static Tailwind stylesheet from frontend/build/. Deliberately NOT
# the cdn.tailwindcss.com Play CDN: that script JIT-compiles every utility
# class at runtime via eval()/new Function(), so it produces ZERO styles under
# any CSP that blocks 'unsafe-eval' (a common browser/security-extension
# policy) — the page still renders its DOM, just completely unstyled, which
# looks like a blank/broken page. This stage never ships in the final image;
# only its compiled output (frontend/static/tailwind.css) does.
FROM node:20-alpine AS cssbuild
WORKDIR /src/frontend/build
COPY frontend/build/package.json frontend/build/tailwind.config.js frontend/build/input.css ./
RUN npm install --no-audit --no-fund
COPY frontend/templates /src/frontend/templates
COPY frontend/static/app.js /src/frontend/static/app.js
RUN npx tailwindcss -i ./input.css -o ../static/tailwind.css --minify

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

# Runtime packages. Mirrors setup.sh's apt list (nginx + stream module,
# python3, iproute2, dhcp client, openssl, iptables) plus the CLI tools the
# Tools page shells out to (ping/traceroute/tcpdump/whois/dig/netstat) and
# procps for `pgrep`, which nginx_manager uses to detect a running nginx.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      nginx libnginx-mod-stream \
      python3 python3-venv \
      iproute2 isc-dhcp-client openssl iptables \
      procps iputils-ping traceroute tcpdump whois dnsutils net-tools \
      ca-certificates certbot \
 && rm -rf /var/lib/apt/lists/*

# Python deps in an isolated venv (Debian bookworm is PEP-668 "externally
# managed", so we don't install into the system interpreter).
COPY backend/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
ENV PATH="/opt/venv/bin:$PATH"

# App source (backend/ and frontend/ must stay siblings — Flask looks for
# templates at ../frontend/templates relative to backend/app.py).
WORKDIR /opt/splitter
COPY backend/ /opt/splitter/backend/
COPY frontend/ /opt/splitter/frontend/
# Always ship the freshly-built stylesheet, even if a locally-generated
# frontend/static/tailwind.css happened to be present (and possibly stale) in
# the build context above.
COPY --from=cssbuild /src/frontend/static/tailwind.css /opt/splitter/frontend/static/tailwind.css
COPY setup.sh /opt/splitter/setup.sh
COPY docker/entrypoint.sh /opt/splitter/entrypoint.sh
RUN chmod +x /opt/splitter/entrypoint.sh /opt/splitter/setup.sh

# nginx wiring: create the dirs the app writes into and add the top-level
# stream {} include (the same block setup.sh appends to nginx.conf). The stream
# module itself auto-loads from /etc/nginx/modules-enabled (libnginx-mod-stream).
# Drop Debian's default site so nginx doesn't grab host port 80 under host net.
RUN mkdir -p /etc/nginx/stream.d /etc/nginx/ssl /etc/nginx/acl.d \
             /etc/nginx/conf.d /etc/nginx/modsec \
 && chmod 755 /etc/nginx/stream.d /etc/nginx/acl.d \
 && chmod 700 /etc/nginx/ssl \
 && rm -f /etc/nginx/sites-enabled/default \
 && printf '\n# >>> splitter >>>\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}\n# <<< splitter <<<\n' \
      >> /etc/nginx/nginx.conf

# WAF (ModSecurity + OWASP CRS) baked into the image.
# The dashboard's WAF "Install" runs `setup.sh --waf-only`, which sets up the
# ModSecurity connector, the OWASP CRS and the base config under image-layer
# paths (/etc/nginx/modsec, /usr/lib/nginx/modules, modules-enabled). Those do
# NOT persist across container recreation, so a runtime-only install would
# dangle after the next `docker compose up --build` and break nginx. We run that
# exact setup.sh path ONCE here at build time so the connector, CRS and base
# config live permanently in the image, then delete only the generated server
# block so the WAF starts OFF. Clicking Install in the UI re-creates that block
# (into the conf.d volume) against the already-present module, so it works and
# stays consistent across every redeploy.
#
# On Debian bookworm the connector is a real package (libnginx-mod-http-
# modsecurity), which setup.sh detects and uses — no compile needed. We KEEP
# that package (and the apt lists) so the UI's re-run of setup.sh sees it already
# installed and takes the same clean package path, rather than trying to add a
# second load_module (which would fail nginx -t with "module already loaded").
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    nginx; \
    bash /opt/splitter/setup.sh --waf-only; \
    nginx -s stop; \
    rm -f /etc/nginx/conf.d/splitter-waf.conf /etc/nginx/conf.d/splitter-waf-proxy.inc; \
    rm -rf /tmp/*; \
    nginx -t

# Splitter config (override any of these in docker-compose.yml).
# SPLITTER_NIC=auto → the entrypoint detects the host uplink at runtime from the
# default route (works because of host networking). Pin a name only if you have
# several uplinks and want a specific one.
ENV SPLITTER_SIMULATE=0 \
    SPLITTER_DATA_DIR=/var/lib/splitter \
    SPLITTER_HOST=0.0.0.0 \
    SPLITTER_PORT=8088 \
    SPLITTER_NIC=auto \
    SPLITTER_BIND_PREFIX=24 \
    SPLITTER_SUDO=""

EXPOSE 8088
ENTRYPOINT ["/opt/splitter/entrypoint.sh"]
