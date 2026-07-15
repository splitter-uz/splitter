"""
WAF control — manage the ModSecurity + OWASP CRS reverse proxy from the UI.

The WAF (installed by setup.sh) is an nginx server block that terminates TLS,
runs every request through ModSecurity/CRS, and proxies to the app on loopback.
This module is the dashboard's control plane over it:

  * install / repair the WAF from the UI (runs `setup.sh --waf-only`)
  * read status (installed? engine mode? listen port? recent blocks?)
  * switch the engine mode  Off / DetectionOnly / On   (the "enforce" switch)
  * re-render the server block when the port or rate limits change
  * surface the most recent ModSecurity events from the audit log

Like nginx_manager / net_settings it is SIMULATE-aware: off a real host it
reads/writes stub copies under DATA_DIR so the whole page round-trips in dev.
"""
import os
import re
import subprocess
import tempfile
import threading
import time

import config
import storage
import nginx_manager as nm

# Repo root (backend/waf.py -> backend -> repo), where setup.sh lives.
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Background install state, shared across requests. The dashboard polls it.
_INSTALL = {"running": False, "ok": None, "log": "", "started": 0.0}
_INSTALL_LOCK = threading.Lock()
from nginx_manager import StepResult

_MODE_RE = re.compile(r"^\s*SecRuleEngine\s+(\w+)", re.MULTILINE)
VALID_MODES = ("Off", "DetectionOnly", "On")

# ModSecurity alert line, e.g.
#   ModSecurity: Warning. ... [id "942100"] [msg "SQL Injection"] ... [uri "/x"]
# Each bracketed field is optional and ordering varies, so pull them out one by
# one rather than with a single brittle pattern.
_ACTION_RE = re.compile(r'ModSecurity:\s*(\w+)')
_FIELD_RE = {
    "id": re.compile(r'\[id "([^"]*)"\]'),
    "msg": re.compile(r'\[msg "([^"]*)"\]'),
    "severity": re.compile(r'\[severity "([^"]*)"\]'),
    "uri": re.compile(r'\[uri "([^"]*)"\]'),
    "hostname": re.compile(r'\[hostname "([^"]*)"\]'),
}

# The audit log is written in Serial format (SecAuditLogType Serial — see
# setup.sh), i.e. one block of lettered sections per transaction:
#
#   ---uniqueid---A--
#   [14/Jul/2026:10:00:00 +0000] uid 10.0.0.5 54321 192.0.2.7 8443
#   ---uniqueid---B--
#   GET /login HTTP/1.1
#   ---uniqueid---H--
#   ModSecurity: Warning. ... [id "920350"] ... [uri "/login"]
#   ---uniqueid---Z--
#
# The alert lines (section H) carry the rule/message/uri but NOT the client IP —
# that only appears on the section-A audit header. So the whole block has to be
# parsed together and the A-section IP attached to each of the block's alerts.
# Boundary syntax differs between ModSecurity v2 (--id-A--) and v3
# (---id---A--), hence the loose dash counts.
_BOUNDARY_RE = re.compile(r'^-{2,}[A-Za-z0-9]+-{1,}([A-Z])--\s*$')

# Section A header: [timestamp] unique_id client_ip client_port server_ip server_port
_SECTION_A_RE = re.compile(
    r'^\[(?P<time>[^\]]+)\]\s+\S+\s+(?P<client_ip>\S+)\s+\d+\s+\S+\s+\d+')

# Fallback: when the alert is read straight from nginx's error log rather than
# the audit log, the client is appended to the same line as ", client: 1.2.3.4".
_ERRORLOG_CLIENT_RE = re.compile(r'\bclient:\s*([^,\s]+)')


# --------------------------------------------------------------------------
# SIMULATE-aware file access (same pattern as net_settings)
# --------------------------------------------------------------------------
def _stub(path):
    return os.path.join(config.DATA_DIR, "wafstub_" + os.path.basename(path))


def _target(path):
    """Real path on the host; a seeded DATA_DIR stub in simulation."""
    if not config.SIMULATE:
        return path
    stub = _stub(path)
    if not os.path.exists(stub):
        os.makedirs(config.DATA_DIR, exist_ok=True)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as s, \
                        open(stub, "w", encoding="utf-8") as d:
                    d.write(s.read())
            except OSError:
                pass
        else:
            _seed_stub(path, stub)
    return stub


def _seed_stub(path, stub):
    """Give the dev box a believable WAF so the page is usable in simulation."""
    seed = ""
    if path == config.WAF_MODSEC_CONF:
        seed = ("SecRuleEngine DetectionOnly\nSecRequestBodyAccess On\n"
                "SecAuditEngine RelevantOnly\n"
                "SecAuditLog /var/log/modsec_audit.log\n")
    elif path == config.WAF_CONF:
        seed = render_conf_text(_settings())
    elif path == config.WAF_AUDIT_LOG:
        # Mirrors the real Serial-format audit log (see recent_events) so the
        # dev box exercises the same parser, client IP included.
        seed = (
            '---a1b2c3d4---A--\n'
            '[26/Jun/2026:09:14:02 +0000] a1b2c3d4 203.0.113.14 51224 192.0.2.7 8443\n'
            '---a1b2c3d4---B--\n'
            'POST /api/login HTTP/1.1\n'
            '---a1b2c3d4---H--\n'
            'ModSecurity: Warning. detected SQLi [id "942100"] '
            '[msg "SQL Injection Attack Detected via libinjection"] '
            '[severity "2"] [uri "/api/login"]\n'
            '---a1b2c3d4---Z--\n'
            '---e5f6a7b8---A--\n'
            '[26/Jun/2026:09:15:31 +0000] e5f6a7b8 198.51.100.42 40118 192.0.2.7 8443\n'
            '---e5f6a7b8---B--\n'
            'GET / HTTP/1.1\n'
            '---e5f6a7b8---H--\n'
            'ModSecurity: Warning. XSS [id "941100"] '
            '[msg "XSS Attack Detected"] [severity "2"] [uri "/"]\n'
            '---e5f6a7b8---Z--\n')
    if seed:
        try:
            with open(stub, "w", encoding="utf-8") as fh:
                fh.write(seed)
        except OSError:
            pass


def _read(path):
    try:
        with open(_target(path), "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _write(label, path, text):
    target = _target(path)
    directory = os.path.dirname(target) or "."
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
        os.chmod(target, 0o644)
    except OSError as exc:
        return StepResult(label, False, str(exc))
    note = "[simulated] " if config.SIMULATE else ""
    return StepResult(label, True, f"{note}wrote {target}", simulated=config.SIMULATE)


# --------------------------------------------------------------------------
# Settings (port + rate limits persist in the app store; mode lives in the
# ModSecurity config file, which is the source of truth on the host).
# --------------------------------------------------------------------------
def _settings():
    s = storage.settings_all()
    return {
        "port": int(s.get("waf_port", config.WAF_DEFAULT_PORT)),
        "login_rate": s.get("waf_login_rate", "10r/m"),
        "api_rate": s.get("waf_api_rate", "30r/s"),
    }


def engine_mode():
    """Current SecRuleEngine value, or None if the WAF is not installed."""
    m = _MODE_RE.search(_read(config.WAF_MODSEC_CONF))
    return m.group(1) if m else None


def installed():
    return bool(_read(config.WAF_CONF).strip())


def status():
    st = _settings()
    return {
        "installed": installed(),
        "mode": engine_mode(),
        "simulate": config.SIMULATE,
        "upstream": config.WAF_UPSTREAM,
        "audit_log": config.WAF_AUDIT_LOG,
        "app_host": config.HOST,
        "app_loopback": config.HOST in ("127.0.0.1", "localhost", "::1"),
        "install": install_state(),
        **st,
    }


# --------------------------------------------------------------------------
# Install / repair — runs `setup.sh --waf-only` in the background so the
# dashboard's Install/Repair button never blocks the request. Progress is
# exposed via install_state() (folded into status()).
# --------------------------------------------------------------------------
def install_state():
    with _INSTALL_LOCK:
        return dict(_INSTALL)


def _set_install(**kw):
    with _INSTALL_LOCK:
        _INSTALL.update(kw)


def _run_install():
    script = os.path.join(REPO_DIR, "setup.sh")
    try:
        # Pass the app's real port so the WAF proxies to the right place even
        # when SPLITTER_PORT isn't the 8088 default.
        proc = subprocess.Popen(
            ["bash", script, "--waf-only", "--port", str(config.PORT)],
            cwd=REPO_DIR,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        lines = []
        for line in proc.stdout:
            lines.append(line)
            _set_install(log="".join(lines[-250:]))   # keep the tail
        proc.wait()
        _set_install(ok=(proc.returncode == 0))
    except Exception as exc:                            # noqa: BLE001
        with _INSTALL_LOCK:
            _INSTALL["ok"] = False
            _INSTALL["log"] += f"\n[error] {exc}"
    finally:
        _set_install(running=False)


def install():
    """Kick off a background WAF (re)install. Idempotent while one is running."""
    with _INSTALL_LOCK:
        if _INSTALL["running"]:
            return {"ok": True, "running": True, "note": "install already running"}
        _INSTALL.update(running=True, ok=None, log="", started=time.time())

    if config.SIMULATE:
        # Dev box: don't shell out — the stubs already make the WAF "installed".
        def _sim():
            time.sleep(1.0)
            _set_install(running=False, ok=True,
                         log="[simulated] WAF (re)installed — DetectionOnly.")
        threading.Thread(target=_sim, daemon=True).start()
        return {"ok": True, "running": True}

    threading.Thread(target=_run_install, daemon=True).start()
    return {"ok": True, "running": True}


# --------------------------------------------------------------------------
# Render the nginx server block from the current settings
# --------------------------------------------------------------------------
def render_conf_text(s):
    port = s["port"]
    return f"""# Splitter WAF — rendered by the dashboard (backend/waf.py). Edits here are
# overwritten when you change WAF settings in the UI.
limit_req_zone $binary_remote_addr zone=splitter_login:10m rate={s['login_rate']};
limit_req_zone $binary_remote_addr zone=splitter_api:10m rate={s['api_rate']};

server {{
    listen {port} ssl;
    listen [::]:{port} ssl;
    server_name _;

    ssl_certificate     {config.SSL_DIR}/splitter-waf.crt;
    ssl_certificate_key {config.SSL_DIR}/splitter-waf.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_prefer_server_ciphers on;

    modsecurity on;
    modsecurity_rules_file {config.WAF_MODSEC_DIR}/main.conf;

    add_header X-Frame-Options           "DENY"        always;
    add_header X-Content-Type-Options    "nosniff"     always;
    add_header Referrer-Policy           "same-origin" always;
    add_header Strict-Transport-Security "max-age=31536000" always;
    client_max_body_size 2m;

    location = /api/login {{
        limit_req zone=splitter_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://{config.WAF_UPSTREAM};
        include {os.path.dirname(config.WAF_CONF)}/splitter-waf-proxy.inc;
    }}
    location = /api/setup {{
        limit_req zone=splitter_login burst=5 nodelay;
        limit_req_status 429;
        proxy_pass http://{config.WAF_UPSTREAM};
        include {os.path.dirname(config.WAF_CONF)}/splitter-waf-proxy.inc;
    }}
    location / {{
        limit_req zone=splitter_api burst=60 nodelay;
        limit_req_status 429;
        proxy_pass http://{config.WAF_UPSTREAM};
        include {os.path.dirname(config.WAF_CONF)}/splitter-waf-proxy.inc;
    }}
}}
"""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
_RATE_RE = re.compile(r"^\d+r/[sm]$")


def _valid_rate(v):
    return bool(_RATE_RE.match((v or "").strip()))


# --------------------------------------------------------------------------
# Mutations
# --------------------------------------------------------------------------
def set_mode(mode):
    """Switch SecRuleEngine (Off / DetectionOnly / On) and reload nginx."""
    if mode not in VALID_MODES:
        return False, [StepResult("Set WAF mode", False,
                                  f"Invalid mode {mode!r} (Off/DetectionOnly/On)")]
    conf = _read(config.WAF_MODSEC_CONF)
    if not conf.strip():
        return False, [StepResult("Set WAF mode", False,
                                  "WAF not installed — run sudo ./setup.sh")]
    if _MODE_RE.search(conf):
        conf = _MODE_RE.sub(f"SecRuleEngine {mode}", conf, count=1)
    else:
        conf = f"SecRuleEngine {mode}\n" + conf
    steps = [_write(f"Set SecRuleEngine {mode}", config.WAF_MODSEC_CONF, conf)]
    if steps[-1]["ok"]:
        steps.append(_nginx_test())
        if steps[-1]["ok"]:
            steps.append(nm.reload_nginx())
    return all(s["ok"] for s in steps), steps


def apply_settings(patch):
    """Persist port/rate changes, re-render the server block, test + reload."""
    s = _settings()
    if "port" in patch:
        try:
            port = int(patch["port"])
        except (TypeError, ValueError):
            return False, [StepResult("Apply WAF settings", False, "Port must be a number")]
        if not (1 <= port <= 65535):
            return False, [StepResult("Apply WAF settings", False, "Port out of range")]
        s["port"] = port
    for key in ("login_rate", "api_rate"):
        if key in patch:
            if not _valid_rate(patch[key]):
                return False, [StepResult("Apply WAF settings", False,
                                          f"Invalid rate {patch[key]!r} (e.g. 10r/m, 30r/s)")]
            s[key] = patch[key].strip()

    storage.settings_update({
        "waf_port": s["port"],
        "waf_login_rate": s["login_rate"],
        "waf_api_rate": s["api_rate"],
    })
    steps = [_write("Render WAF server block", config.WAF_CONF, render_conf_text(s))]
    if steps[-1]["ok"]:
        steps.append(_nginx_test())
        if steps[-1]["ok"]:
            steps.append(nm.reload_nginx())
    return all(s2["ok"] for s2 in steps), steps


def _nginx_test():
    return nm.run_cmd("Validate nginx (-t)",
                      ([config.NGINX_BIN, "-t"] if not config.SUDO
                       else config.SUDO.split() + [config.NGINX_BIN, "-t"]),
                      simulate_out="nginx: configuration file test is successful")


# --------------------------------------------------------------------------
# Recent events from the audit log
# --------------------------------------------------------------------------
def _parse_alert(line, client_ip="", time=""):
    """Turn one 'ModSecurity: ...' alert line into an event dict."""
    action = _ACTION_RE.search(line)
    ev = {"action": action.group(1) if action else ""}
    for key, rx in _FIELD_RE.items():
        m = rx.search(line)
        ev[key] = m.group(1) if m else ""
    # An alert read from nginx's error log carries its own client on the line;
    # one from the audit log inherits the section-A client of its transaction.
    if not client_ip:
        m = _ERRORLOG_CLIENT_RE.search(line)
        client_ip = m.group(1) if m else ""
    ev["client_ip"] = client_ip
    ev["time"] = time
    return ev


def recent_events(limit=50):
    """Most recent ModSecurity alerts, newest first.

    Walks the Serial-format audit log transaction by transaction so each alert
    (section H) can be tagged with the client IP + timestamp from its own
    transaction's audit header (section A). Alert lines that appear outside any
    block (e.g. when the log is really nginx's error log) still parse, taking
    their client from the line itself.
    """
    text = _read(config.WAF_AUDIT_LOG)
    events = []
    client_ip = ""   # of the transaction currently being read
    time = ""
    section = None

    for line in text.splitlines():
        boundary = _BOUNDARY_RE.match(line)
        if boundary:
            section = boundary.group(1)
            if section == "A":
                # A new transaction starts: drop the previous one's client.
                client_ip, time = "", ""
            continue

        if section == "A" and not client_ip:
            m = _SECTION_A_RE.match(line)
            if m:
                client_ip = m.group("client_ip")
                time = m.group("time")
            continue

        if "ModSecurity:" in line:
            events.append(_parse_alert(line, client_ip, time))

    events.reverse()  # newest first
    return events[:limit]
