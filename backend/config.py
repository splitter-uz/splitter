"""
Central configuration for Splitter.

Every value can be overridden with an environment variable so the same code
runs unchanged on a developer laptop (simulation) and on the real Nginx host.
"""
import os
import platform
import shutil


def _env_bool(name, default):
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# --- Filesystem locations -------------------------------------------------
# Directory that Nginx's `stream {}` block includes via `include .../*.conf;`
STREAM_CONF_DIR = os.environ.get("SPLITTER_STREAM_DIR", "/etc/nginx/stream.d")

# Where uploaded certificates and keys are stored.
SSL_DIR = os.environ.get("SPLITTER_SSL_DIR", "/etc/nginx/ssl")

# Directory holding per-access-list nginx snippets (allow/deny rules). Each list
# is one <name>.conf here; a mapping's stream server block pulls in the one it
# selected via `include`. Kept OUT of STREAM_CONF_DIR so the `stream.d/*.conf`
# glob never loads these at stream{} top level (where allow/deny is invalid).
ACL_CONF_DIR = os.environ.get("SPLITTER_ACL_DIR", "/etc/nginx/acl.d")

# Global nginx error log. The per-mapping "Diagnose" panel tails this file
# (root-owned, so read via sudo) and filters it down to the lines that mention
# the mapping's listen IP or one of its upstreams.
NGINX_ERROR_LOG = os.environ.get("SPLITTER_NGINX_ERROR_LOG", "/var/log/nginx/error.log")

# Filesystem whose usage the Monitoring page reports (root by default).
MONITOR_DISK_PATH = os.environ.get("SPLITTER_MONITOR_DISK_PATH", "/")

# Per-mapping traffic logs. Each mapping gets its own directory
# <LOG_DIR>/<domain>.<port>/ holding <domain>.<port>-access.log and
# <domain>.<port>-error.log, written by nginx and browsed from the Logs page.
LOG_DIR = os.environ.get("SPLITTER_LOG_DIR", "/var/log/splitter")

# Host network-settings files editable from the Network page.
RESOLV_CONF = os.environ.get("SPLITTER_RESOLV_CONF", "/etc/resolv.conf")
HOSTS_FILE = os.environ.get("SPLITTER_HOSTS_FILE", "/etc/hosts")

# Persistent record of every mapping the tool manages.
#
# IMPORTANT: this must live OUTSIDE the app directory, otherwise a redeploy
# (fresh clone / replaced folder) wipes the mappings. As root we default to
# /var/lib/splitter; for non-root dev we fall back to <repo>/backend/data.
def _default_data_dir():
    env = os.environ.get("SPLITTER_DATA_DIR")
    if env:
        return env
    if os.geteuid() == 0:
        return "/var/lib/splitter"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


DATA_DIR = _default_data_dir()
DB_FILE = os.path.join(DATA_DIR, "mappings.json")

# --- Network --------------------------------------------------------------
# Network interface the new bind IPs are attached to (STEP A).
NIC = os.environ.get("SPLITTER_NIC", "eth0")
# CIDR prefix used when binding an address with `ip addr add`.
BIND_PREFIX = os.environ.get("SPLITTER_BIND_PREFIX", "24")

# Create a labeled sub-interface (alias) for every STATIC bind IP, e.g.
#   ip addr add <ip>/<prefix> dev eth0 label eth0:0
# so each mapping's real IP shows up as its own sub-interface (eth0:0, eth0:1…).
USE_ALIAS = _env_bool("SPLITTER_USE_ALIAS", True)

# For DHCP allocation a macvlan sub-interface is created on the parent NIC and a
# DHCP client is run on it to obtain a real lease. Name = <prefix><index>.
MACVLAN_PREFIX = os.environ.get("SPLITTER_MACVLAN_PREFIX", "strm")
MACVLAN_MODE = os.environ.get("SPLITTER_MACVLAN_MODE", "bridge")

# Sub-interface type: "macvlan" (own MAC, blocked by VMware Forged-Transmits policy),
# "ipvlan" (shares parent MAC, works on VMware), or "auto" (detects VMware by OUI).
SUBIFACE_TYPE = os.environ.get("SPLITTER_SUBIFACE_TYPE", "auto")

# DHCP client invocation. {iface} is substituted with the sub-interface name.
# `dhclient -1` makes one attempt; ISC dhclient has no -timeout flag, so the
# overall time is bounded with the coreutils `timeout` wrapper.
# Common alternatives: "dhcpcd -1 -t 20 {iface}", "nmcli device connect {iface}".
DHCP_ACQUIRE_CMD = os.environ.get(
    "SPLITTER_DHCP_ACQUIRE_CMD", "timeout 25 dhclient -1 {iface}")
DHCP_RELEASE_CMD = os.environ.get(
    "SPLITTER_DHCP_RELEASE_CMD", "dhclient -r {iface}")

# Default backend the form pre-fills.
DEFAULT_BACKEND = os.environ.get("SPLITTER_DEFAULT_BACKEND", "192.168.10.10:443")
LISTEN_PORT = int(os.environ.get("SPLITTER_LISTEN_PORT", "443"))

# Default proxy timeouts for the generated stream server block (per-mapping
# overridable from the UI).
PROXY_TIMEOUT = os.environ.get("SPLITTER_PROXY_TIMEOUT", "10m")
PROXY_CONNECT_TIMEOUT = os.environ.get("SPLITTER_PROXY_CONNECT_TIMEOUT", "5s")

# DNS resolver a forward proxy uses to look up its dynamic (SNI-decided) relay
# targets — required by nginx whenever `proxy_pass` is given a variable rather
# than a literal address/upstream.
FWDPROXY_RESOLVER = os.environ.get("SPLITTER_FWDPROXY_RESOLVER", "8.8.8.8 1.1.1.1")

# --- Behaviour ------------------------------------------------------------
# When a bind IP is removed, also tear it down from the interface.
UNBIND_ON_DELETE = _env_bool("SPLITTER_UNBIND_ON_DELETE", True)
# When a mapping is removed, delete its uploaded cert/key too.
REMOVE_CERTS_ON_DELETE = _env_bool("SPLITTER_REMOVE_CERTS_ON_DELETE", True)

# How Nginx is reloaded/restarted after a config change. Both default to the
# nginx binary (no systemctl): reload = `nginx -s reload`, restart = a full
# `nginx -s stop` + `nginx` start. Driving nginx directly re-binds listen
# sockets reliably and avoids systemd not picking up freshly-added bind IPs.
# Override with a service command if you prefer, e.g. "systemctl reload nginx".
NGINX_BIN = os.environ.get("SPLITTER_NGINX_BIN", "nginx")
RELOAD_CMD = os.environ.get("SPLITTER_RELOAD_CMD", "")   # "" => nginx -s reload
RESTART_CMD = os.environ.get("SPLITTER_RESTART_CMD", "")  # "" => nginx -s stop + start

# Let nginx bind an IP that isn't fully "up" yet. A reload/restart immediately
# after `ip addr add` can otherwise fail with "bind() ... Cannot assign
# requested address", while a later manual restart succeeds once the address
# settles. Enabling net.ipv4.ip_nonlocal_bind removes that race.
IP_NONLOCAL_BIND = _env_bool("SPLITTER_IP_NONLOCAL_BIND", True)

# Command used to reboot the whole host from the dashboard (admin only). Runs
# through the privileged (sudo) prefix like every other system command. Override
# for a non-systemd init if needed, e.g. "reboot".
REBOOT_CMD = os.environ.get("SPLITTER_REBOOT_CMD", "shutdown -r now")

# --- WAF (ModSecurity + OWASP CRS) ---------------------------------------
# The WAF reverse-proxies to the app and filters requests. These paths mirror
# what setup.sh lays down on the host. The dashboard reads status from them and
# renders the server block + engine mode.
WAF_MODSEC_DIR = os.environ.get("SPLITTER_WAF_MODSEC_DIR", "/etc/nginx/modsec")
WAF_MODSEC_CONF = os.path.join(WAF_MODSEC_DIR, "modsecurity.conf")
WAF_CRS_DIR = os.path.join(WAF_MODSEC_DIR, "crs")
WAF_MODULE_SO = os.environ.get(
    "SPLITTER_WAF_MODULE",
    "/usr/lib/nginx/modules/ngx_http_modsecurity_module.so")
WAF_CONF = os.environ.get("SPLITTER_WAF_CONF",
                          "/etc/nginx/conf.d/splitter-waf.conf")
# Where per-app L7 reverse-proxy+WAF server blocks are written. conf.d is
# already included by nginx's http{} (same place the WAF server block lives), so
# a mapping "bound" to the WAF renders here instead of the stream.d directory.
WAF_APP_CONF_DIR = os.environ.get("SPLITTER_WAF_APP_DIR",
                                  os.path.dirname(WAF_CONF))
WAF_AUDIT_LOG = os.environ.get("SPLITTER_WAF_AUDIT_LOG",
                               "/var/log/modsec_audit.log")
# Defaults for the rendered server block (all editable from the dashboard).
WAF_DEFAULT_PORT = int(os.environ.get("SPLITTER_WAF_PORT", "8443"))
# The app the WAF proxies to — its own loopback address:port (defaults to the
# UI port so a non-default SPLITTER_PORT still works).
WAF_UPSTREAM = os.environ.get(
    "SPLITTER_WAF_UPSTREAM",
    "127.0.0.1:" + os.environ.get("SPLITTER_PORT", "8088"))

# --- Let's Encrypt (certbot) ----------------------------------------------
# HTTP-01 only: certbot's --standalone authenticator binds the challenge
# directly on the mapping's own bind IP:80, so no nginx webroot wiring is
# needed. The issued cert is copied into SSL_DIR alongside uploaded/self-signed
# ones, so every other code path (render_conf, the mapping form's cert picker,
# …) treats it identically regardless of how it got there.
CERTBOT_BIN = os.environ.get("SPLITTER_CERTBOT_BIN", "certbot")
LETSENCRYPT_STAGING_DEFAULT = _env_bool("SPLITTER_LETSENCRYPT_STAGING", False)
# Re-checked periodically; a cert is renewed once it's within this many days
# of expiring (mirrors certbot's/Let's Encrypt's own 30-day guidance).
LETSENCRYPT_RENEW_WITHIN_DAYS = int(os.environ.get("SPLITTER_LETSENCRYPT_RENEW_DAYS", "30"))

# Prefix used for privileged commands. Empty when already running as root
# (the systemd unit sets SPLITTER_SUDO=""); falls back to "sudo" otherwise.
SUDO = os.environ.get("SPLITTER_SUDO", "" if os.geteuid() == 0 else "sudo")

# --- Simulation mode ------------------------------------------------------
# In simulation mode no privileged system command is executed; the command
# that *would* run is logged and reported back to the UI. This lets the whole
# tool be demonstrated/tested off the production host.
def _auto_simulate():
    # Force on/off explicitly if the operator set the variable.
    if "SPLITTER_SIMULATE" in os.environ:
        return _env_bool("SPLITTER_SIMULATE", False)
    # Otherwise auto-detect: simulate when we are clearly not on a configured
    # Linux Nginx host.
    if platform.system() != "Linux":
        return True
    if shutil.which("ip") is None or shutil.which(NGINX_BIN) is None:
        return True
    return False


SIMULATE = _auto_simulate()

# Max upload size for cert/key files (bytes).
MAX_UPLOAD_BYTES = int(os.environ.get("SPLITTER_MAX_UPLOAD", str(256 * 1024)))

# Flask
def _persistent_secret_key():
    """Session-signing key. Prefer an explicit env override; otherwise persist a
    generated key under DATA_DIR so logins survive a service restart / host
    reboot (a fresh random key each start would invalidate every session)."""
    env = os.environ.get("SPLITTER_SECRET_KEY")
    if env:
        return env
    path = os.path.join(DATA_DIR, "secret_key")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = os.urandom(24).hex()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key)
    except OSError:
        pass  # fall back to an in-memory key (sessions won't survive a restart)
    return key


SECRET_KEY = _persistent_secret_key()
HOST = os.environ.get("SPLITTER_HOST", "0.0.0.0")
PORT = int(os.environ.get("SPLITTER_PORT", "8088"))


def as_dict():
    """Subset of config that is safe to expose to the frontend."""
    return {
        "stream_conf_dir": STREAM_CONF_DIR,
        "ssl_dir": SSL_DIR,
        "acl_conf_dir": ACL_CONF_DIR,
        "nic": NIC,
        "bind_prefix": BIND_PREFIX,
        "use_alias": USE_ALIAS,
        "macvlan_prefix": MACVLAN_PREFIX,
        "default_backend": DEFAULT_BACKEND,
        "listen_port": LISTEN_PORT,
        "simulate": SIMULATE,
        "letsencrypt_staging_default": LETSENCRYPT_STAGING_DEFAULT,
    }
