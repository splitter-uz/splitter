#!/bin/sh
# Splitter container entrypoint.
#
# Starts nginx as a background daemon (the Flask app reloads/restarts it on
# every Apply via `nginx -s reload` / stop+start), then runs the app in the
# foreground as the container's main process.
set -e

DATA_DIR="${SPLITTER_DATA_DIR:-/var/lib/splitter}"
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR" 2>/dev/null || true

# Auto-detect the host uplink interface unless the operator pinned one.
# Under host networking the container shares the host's routing table, so the
# default route names the real uplink (e.g. ens33). This is the interface the
# UI pre-selects; the Interfaces page still discovers every NIC dynamically.
if [ -z "$SPLITTER_NIC" ] || [ "$SPLITTER_NIC" = "auto" ]; then
  nic="$(ip route show default 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="dev"){print $(i+1); exit}}')"
  if [ -z "$nic" ]; then
    # No default route — pick the first real UP interface, skipping loopback,
    # docker/bridge/veth, and Splitter's own macvlan sub-interfaces (mv-*).
    nic="$(ip -o link show up 2>/dev/null \
      | awk -F': ' '{print $2}' | sed 's/@.*//' \
      | grep -Ev '^(lo|docker[0-9]*|br-|veth|mv-)' | head -n1)"
  fi
  if [ -n "$nic" ]; then
    export SPLITTER_NIC="$nic"
    echo " * auto-detected uplink NIC: $SPLITTER_NIC"
  else
    export SPLITTER_NIC="eth0"
    echo " ! could not auto-detect a NIC; falling back to eth0" >&2
  fi
fi

# Defensive: ensure the top-level stream include exists (normally baked in the
# image, but a mounted nginx.conf could be missing it).
if ! grep -q 'stream.d/\*.conf' /etc/nginx/nginx.conf 2>/dev/null; then
  printf '\nstream {\n    include /etc/nginx/stream.d/*.conf;\n}\n' >> /etc/nginx/nginx.conf
fi

# Let nginx bind an IP that isn't fully "up" yet (matches setup.sh's
# net.ipv4.ip_nonlocal_bind=1). Needs a writable /proc/sys — i.e. `privileged:
# true`. Under host networking this sets the value on the shared host netns.
# Best-effort: if it fails (least-privilege setup), set it on the HOST instead.
if [ "${SPLITTER_IP_NONLOCAL_BIND:-1}" = "1" ]; then
  sysctl -w net.ipv4.ip_nonlocal_bind=1 >/dev/null 2>&1 \
    || echo " ! could not set net.ipv4.ip_nonlocal_bind (set it on the host, or run privileged)" >&2
fi

# Recreate the per-mapping log directories referenced by the persisted configs.
# The conf volumes (stream.d/conf.d) survive a container recreate, but the log
# tree may not — and nginx refuses to start if an access_log/error_log
# directory is missing.
LOG_DIR="${SPLITTER_LOG_DIR:-/var/log/splitter}"
mkdir -p "$LOG_DIR"
for f in /etc/nginx/stream.d/*.conf /etc/nginx/conf.d/*.conf; do
  [ -f "$f" ] || continue
  awk '$1 == "access_log" || $1 == "error_log" {print $2}' "$f"
done | grep '^/' | while read -r logpath; do
  mkdir -p "$(dirname "$logpath")"
done

# Validate the baked config, then start nginx (daemon mode). Empty stream.d is
# fine — a glob include with no matches is valid.
nginx -t
nginx
echo " * nginx started"

exec python /opt/splitter/backend/app.py
