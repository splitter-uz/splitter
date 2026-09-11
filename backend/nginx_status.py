"""
nginx stub_status metrics for the Monitoring page.

nginx ships a tiny built-in status endpoint (ngx_http_stub_status_module) that
reports live connection and request counters for the http{} layer — i.e. the
Reverse Proxy / WAF side of Splitter. (Layer-4 stream{} proxies are not counted;
their per-mapping connection counts come from /api/traffic instead.)

This module:
  * renders a loopback-only server block that exposes `/stub_status` on
    127.0.0.1:<STATUS_PORT> and writes it into conf.d (the same directory the
    WAF server block lives in, already included by nginx's http{} and persisted
    by the Docker volume). The file is validated with `nginx -t` before nginx is
    reloaded; on failure it is removed again so nginx never breaks over it.
  * polls that endpoint and turns the monotonically-growing counters into
    per-second rates by diffing against the previous sample.
"""
import os
import re
import threading
import time
import urllib.request

import config
import nginx_manager as nm

STATUS_HOST = "127.0.0.1"
STATUS_PORT = int(os.environ.get("SPLITTER_STATUS_PORT", "8090"))
CONF_PATH = os.path.join(config.WAF_APP_CONF_DIR, "splitter-status.conf")
URL = f"http://{STATUS_HOST}:{STATUS_PORT}/stub_status"

_lock = threading.Lock()
_prev = None   # (monotonic_ts, accepts, handled, requests) from the last poll


def render_conf():
    return f"""# Managed by Splitter — nginx stub_status for the Monitoring page.
# Loopback only; nothing outside this host can reach it.
server {{
    listen {STATUS_HOST}:{STATUS_PORT};
    server_name _;
    access_log off;

    location = /stub_status {{
        stub_status;
        allow 127.0.0.1;
        deny all;
    }}
    location / {{
        return 404;
    }}
}}
"""


def provisioned():
    if config.SIMULATE:
        return True
    try:
        with open(CONF_PATH, encoding="utf-8") as fh:
            return "stub_status" in fh.read()
    except OSError:
        return False


def ensure_conf(force=False):
    """Write the status server block if it's missing (or `force`), validate and
    reload nginx. Returns (ok, steps). Never raises."""
    steps = []
    content = render_conf()
    if config.SIMULATE:
        steps.append(nm.StepResult("Write nginx stub_status config", True,
                                   f"[simulated] would write {CONF_PATH}\n\n{content}",
                                   simulated=True))
        return True, steps
    try:
        current = open(CONF_PATH, encoding="utf-8").read()
    except OSError:
        current = None
    if current == content and not force:
        steps.append(nm.StepResult("Write nginx stub_status config", True,
                                   f"{CONF_PATH} already up to date"))
        return True, steps
    try:
        os.makedirs(os.path.dirname(CONF_PATH), exist_ok=True)
        with open(CONF_PATH, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.chmod(CONF_PATH, 0o644)
        steps.append(nm.StepResult("Write nginx stub_status config", True, f"wrote {CONF_PATH}"))
    except OSError as exc:
        steps.append(nm.StepResult("Write nginx stub_status config", False, str(exc)))
        return False, steps

    test = nm.test_config()
    steps.append(test)
    if not test["ok"]:
        # Roll back: restore what was there before (or remove) so nginx's next
        # reload/restart isn't broken by our file.
        try:
            if current is None:
                os.remove(CONF_PATH)
            else:
                with open(CONF_PATH, "w", encoding="utf-8") as fh:
                    fh.write(current)
        except OSError:
            pass
        steps.append(nm.StepResult("Roll back stub_status config", True,
                                   "restored previous state after failed nginx -t"))
        return False, steps

    reload = nm.reload_nginx()
    steps.append(reload)
    return bool(reload["ok"]), steps


_RE_ACTIVE = re.compile(r"Active connections:\s*(\d+)")
_RE_TOTALS = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\d+)\s*$", re.M)
_RE_RWW = re.compile(r"Reading:\s*(\d+)\s+Writing:\s*(\d+)\s+Waiting:\s*(\d+)")


def parse(text):
    """Parse stub_status output into ints. Raises ValueError on garbage."""
    a, t, r = _RE_ACTIVE.search(text), _RE_TOTALS.search(text), _RE_RWW.search(text)
    if not (a and t and r):
        raise ValueError("unrecognised stub_status output")
    return {
        "active": int(a.group(1)),
        "accepts": int(t.group(1)), "handled": int(t.group(2)), "requests": int(t.group(3)),
        "reading": int(r.group(1)), "writing": int(r.group(2)), "waiting": int(r.group(3)),
    }


def _with_rates(stats):
    """Attach accepts/requests per second (None until the second sample)."""
    global _prev
    now = time.monotonic()
    stats["accepts_rate"] = stats["requests_rate"] = None
    if _prev:
        dt = now - _prev[0]
        if dt > 0:
            stats["accepts_rate"] = max(0.0, (stats["accepts"] - _prev[1]) / dt)
            stats["requests_rate"] = max(0.0, (stats["requests"] - _prev[3]) / dt)
    _prev = (now, stats["accepts"], stats["handled"], stats["requests"])
    stats["dropped"] = max(0, stats["accepts"] - stats["handled"])
    stats["requests_per_connection"] = (
        round(stats["requests"] / stats["handled"], 2) if stats["handled"] else None)
    return stats


def _simulated():
    import math
    t = time.time()
    base = int(t) % 1_000_000
    accepts = 480_000 + base * 3
    stats = {
        "active": int(40 + 25 * (math.sin(t / 5) + 1)),
        "accepts": accepts, "handled": accepts - 2, "requests": accepts * 4 + base,
        "reading": int(2 + 2 * (math.sin(t / 3) + 1)),
        "writing": int(12 + 8 * (math.cos(t / 4) + 1)),
        "waiting": int(25 + 15 * (math.sin(t / 7) + 1)),
    }
    return _with_rates(stats)


def snapshot():
    """One reading. Always returns a dict with `available`; never raises."""
    with _lock:
        if config.SIMULATE:
            return {"available": True, "provisioned": True, "simulated": True,
                    "url": URL, "stats": _simulated()}
        if not provisioned():
            return {"available": False, "provisioned": False, "simulated": False,
                    "url": URL, "reason": "stub_status server block is not provisioned yet."}
        try:
            with urllib.request.urlopen(URL, timeout=2) as resp:
                text = resp.read(4096).decode("utf-8", errors="replace")
            return {"available": True, "provisioned": True, "simulated": False,
                    "url": URL, "stats": _with_rates(parse(text))}
        except Exception as exc:  # connection refused, timeout, bad body
            return {"available": False, "provisioned": True, "simulated": False,
                    "url": URL, "reason": f"{URL}: {exc}"}
