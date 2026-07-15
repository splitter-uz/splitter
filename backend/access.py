"""
Access lists — IP/CIDR allow lists rendered to nginx snippets.

Each list is one record (see storage.access_*) rendered by nginx_manager to a
`<name>.conf` allow/deny snippet that a mapping's stream server block includes.

A list may be:
  * manual — the user types the CIDRs; `entries` is static.
  * auto-refreshing — `source_url` is set; the entries are re-fetched on an
    interval by the background scheduler (replacing the old iplist.sh cron).

The built-in "tasx" list ships by default: it fetches the Uzbekistan (tas-ix)
networks from the MRLG looking glass exactly like the original iplist.sh.
"""
import datetime
import re
import threading
import time
import urllib.request

import config
import nginx_manager as nm
import storage


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Built-in tas-ix list
# --------------------------------------------------------------------------
# Replicates iplist.sh: POST to the MRLG looking glass, "show route" the full
# BGP table, then scrape the Network column. Verified live: ~537 CIDRs.
TASX_SOURCE_URL = "http://mrlg.tas-ix.uz/index.php"
TASX_POST_BODY = b"bgpr=MRLG&show=2&submit=Submit&args="
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

TASX_DEFAULT = {
    "name": "tasx",
    "label": "tas-ix (Uzbekistan)",
    "builtin": True,
    "entries": [],
    "source_url": TASX_SOURCE_URL,
    "include_private": True,
    "refresh_hours": 24,
    "last_refresh": None,
    "count": 0,
}

# First CIDR token on a line (the Network column of the BGP table). Next-hop /
# From columns are bare host IPs (no /prefix), so they never match.
_CIDR_LINE_RE = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
_PRE_RE = re.compile(r"<pre>(.*?)</pre>", re.S | re.I)


def parse_cidrs(html):
    """Extract the sorted unique CIDR list from an MRLG response body."""
    m = _PRE_RE.search(html)
    body = m.group(1) if m else html
    cidrs = []
    for line in body.splitlines():
        mm = _CIDR_LINE_RE.match(line)
        if mm:
            cidrs.append(mm.group(1))
    return sorted(set(cidrs))


def fetch_entries(rec):
    """Fetch and parse the CIDRs for an auto-refreshing list. Raises on failure;
    callers keep the cached `entries` when this raises."""
    url = rec.get("source_url")
    if not url:
        raise ValueError("list has no source_url")
    # tas-ix MRLG needs the POST body; a generic source_url is fetched with GET.
    data = TASX_POST_BODY if url == TASX_SOURCE_URL else None
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": url,
    })
    with urllib.request.urlopen(req, timeout=40) as resp:
        html = resp.read().decode("utf-8", "replace")
    entries = parse_cidrs(html)
    if not entries:
        raise ValueError("no networks found in response")
    return entries


# --------------------------------------------------------------------------
# Snippet (re)writing + nginx reload
# --------------------------------------------------------------------------
def _reload(steps):
    """nginx -t then reload, appending StepResults. Best-effort in SIMULATE."""
    test = nm.test_config()
    steps.append(test)
    if not test["ok"]:
        return False
    steps.append(nm.reload_nginx())
    return True


def write_snippet(rec, reload=True):
    """Render `rec` to its <name>.conf (and _default.conf if it is the current
    default), optionally validating + reloading nginx. Returns list of steps."""
    steps = []
    _path, res = nm.write_acl(rec)
    steps.append(res)
    if storage.settings_all().get("default_access_list") == rec["name"]:
        _dp, dres = nm.write_default_acl(rec)
        steps.append(dres)
    if reload:
        _reload(steps)
    return steps


def refresh(name):
    """Re-fetch an auto-refreshing list, persist, rewrite its snippet + reload.
    Returns (ok, message, steps)."""
    rec = storage.access_get(name)
    if not rec:
        return False, f"No such access list: {name}", []
    if not rec.get("source_url"):
        return False, f"{name} has no source URL to refresh from.", []
    try:
        entries = fetch_entries(rec)
    except Exception as exc:
        # Keep the cached entries; surface the error.
        return False, f"Fetch failed: {exc}", []
    rec = {**rec, "entries": entries, "count": len(entries),
           "last_refresh": _now(), "updated": _now()}
    storage.access_add(rec)
    steps = write_snippet(rec, reload=True)
    return True, f"Refreshed {name}: {len(entries)} network(s).", steps


def ensure_builtin():
    """Create the built-in tas-ix ("tasx") list on first run if absent, and
    make sure its snippet exists on disk."""
    rec = storage.access_get(TASX_DEFAULT["name"])
    if rec is None:
        rec = {**TASX_DEFAULT, "created": _now(), "updated": _now()}
        storage.access_add(rec)
    # Ensure the snippet file exists (e.g. after a redeploy wiped acl.d).
    try:
        nm.write_acl(rec)
    except Exception:
        pass
    return rec


def sync_all():
    """Re-materialise every access-list snippet + the _default.conf mirror on
    disk (no reload). Makes a fresh boot / redeploy self-healing so nginx never
    fails to start on a missing `include`d acl file."""
    default = storage.settings_all().get("default_access_list", "")
    default_rec = None
    for rec in storage.access_list():
        try:
            nm.write_acl(rec)
        except Exception:
            pass
        if rec["name"] == default:
            default_rec = rec
    try:
        nm.write_default_acl(default_rec)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Background refresh scheduler (mirrors backup.start_scheduler)
# --------------------------------------------------------------------------
def _due(rec):
    if not rec.get("source_url"):
        return False
    hours = int(rec.get("refresh_hours") or 24)
    last = rec.get("last_refresh")
    if not last:
        return True
    try:
        prev = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return True
    age_h = (datetime.datetime.now(datetime.timezone.utc) - prev).total_seconds() / 3600
    return age_h >= hours


def run_due():
    """Refresh every auto-refreshing list whose interval has elapsed."""
    for rec in storage.access_list():
        if _due(rec):
            refresh(rec["name"])


_scheduler_started = False


def start_scheduler():
    """Launch the daemon thread that refreshes due access lists (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                run_due()
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=_loop, name="access-scheduler", daemon=True).start()
