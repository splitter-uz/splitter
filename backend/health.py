"""
Backend host health checks.

Every mapping points at one or more upstream ``host:port`` backends. We probe
each one with a short TCP connect (does the port actually accept connections?)
and roll the per-backend results up into a single traffic-light status per
mapping:

    green  — every (enabled) backend is reachable
    yellow — some, but not all, backends are reachable
    red    — no backend is reachable
    gray   — nothing to check (no enabled backends)

Probe results are cached briefly so the dashboard can poll cheaply, and the
probes run concurrently so one slow/blackholed host doesn't stall the rest.
Per-backend state also remembers *since when* the current up/down state has
held, which the UI shows as an uptime/downtime duration.
"""
import datetime
import http.client
import socket
import ssl
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, wait as _wait_futures

import config
import storage

CONNECT_TIMEOUT = 2.0      # seconds to wait for a TCP connect
HTTP_TIMEOUT = 3.0         # seconds for an HTTP health-check request
CACHE_TTL = 20.0           # seconds a probe result is reused before re-probing.
# Must outlive both the frontend's poll interval (15s — otherwise every poll
# treats the cache as expired and re-probes for no benefit) and the slowest
# probe actually seen here (an unresolvable/slow-DNS host can take ~12s to
# naturally fail, per CHECK_ALL_DEADLINE's comment) — otherwise that probe's
# result goes stale again before the next poll ever gets to use it, and it
# ends up re-probed (slowly, in the background — CHECK_ALL_DEADLINE still
# keeps the response itself fast) on literally every single poll forever
# instead of settling into the cache like every faster backend does.
# Outer safety net for check_all() as a whole. socket.create_connection()'s
# own `timeout` only bounds the TCP handshake — the getaddrinfo() DNS lookup
# it does first is NOT covered by it, so an unresolvable/slow-DNS backend can
# block far longer than CONNECT_TIMEOUT regardless (confirmed directly: one
# real case here took a full 12s despite a 2.0s connect timeout, apparently
# an IPv6 attempt hanging before falling back to IPv4). Without this, one
# such backend stalls the whole /api/health response — and by extension
# every page that waits on it — even though every other probe finished in
# milliseconds.
CHECK_ALL_DEADLINE = CONNECT_TIMEOUT + 1.5

# Probes slower than CHECK_ALL_DEADLINE (see above) don't finish in time to
# get cached by the request that started them — so without tracking them,
# every poll before they finally resolve submits ANOTHER redundant duplicate
# probe against the same slow-to-fail host, and a backend slow enough never
# gets a chance to warm the cache at all. Track in-flight probes by cache key
# so concurrent/rapid check_all() calls share one real probe instead of each
# starting their own.
_inflight = {}   # cache_key -> Future
_MAX_WORKERS = 16

_lock = threading.Lock()
# server "host:port" -> {up, since, checked (monotonic), latency_ms, error}
_state = {}


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _split(server):
    """'host:port' -> (host, port_int). Mirrors validators.clean_backend."""
    host, _, port = server.rpartition(":")
    return host.strip(), int(port)


def _probe(server):
    """One TCP connect. Returns (up: bool, latency_ms: float|None, error: str)."""
    try:
        host, port = _split(server)
    except (ValueError, AttributeError):
        return False, None, "bad address"
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT):
            pass
        return True, round((time.monotonic() - start) * 1000, 1), ""
    except OSError as exc:
        return False, None, (exc.strerror or str(exc) or "unreachable")


def hc_spec(mapping):
    """The HTTP health-check spec for a mapping, or None to use a TCP connect.
    {path, scheme, expect}. UDP mappings never HTTP-probe."""
    if not mapping.get("health_check"):
        return None
    if (mapping.get("transport") or "tcp").lower() == "udp":
        return None
    return {
        "path": mapping.get("health_path") or "/",
        "scheme": (mapping.get("health_scheme") or "http").lower(),
        "expect": mapping.get("health_expect"),   # exact status int, or None => any 2xx/3xx
    }


def _cache_key(server, hc):
    if not hc:
        return server
    return f"{server}|{hc['scheme']}|{hc['path']}|{hc.get('expect') or ''}"


def _probe_http(server, hc):
    """One HTTP(S) GET to the backend's health target. Returns (up, latency_ms, error).

    hc["path"] is either a full custom URL (http://host:port/path — probed exactly)
    or a path (/healthz — probed against this backend's host:port using hc["scheme"]).
    """
    target = hc.get("path") or "/"
    start = time.monotonic()
    conn = None
    try:
        if target.lower().startswith(("http://", "https://")):
            u = urllib.parse.urlparse(target)
            scheme = u.scheme
            host = u.hostname
            port = u.port or (443 if scheme == "https" else 80)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
        else:
            scheme = hc.get("scheme") or "http"
            host, port = _split(server)
            path = target
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=HTTP_TIMEOUT, context=ssl._create_unverified_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=HTTP_TIMEOUT)
        conn.request("GET", path, headers={"User-Agent": "splitter-healthcheck"})
        resp = conn.getresponse()
        code = resp.status
        resp.read()
        expect = hc.get("expect")
        ok = (code == int(expect)) if expect else (200 <= code < 400)
        return ok, round((time.monotonic() - start) * 1000, 1), "" if ok else f"HTTP {code}"
    except Exception as exc:                       # noqa: BLE001
        return False, None, (getattr(exc, "strerror", None) or str(exc) or "unreachable")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _check(server, force=False, hc=None):
    """Probe `server` honoring the cache; update and return its state dict. With
    `hc` set, probe via HTTP GET to the health path instead of a TCP connect."""
    key = _cache_key(server, hc)
    now = time.monotonic()
    with _lock:
        prev = _state.get(key)
        if prev and not force and (now - prev["_mono"]) < CACHE_TTL:
            return {k: v for k, v in prev.items() if not k.startswith("_")}

    up, latency, error = _probe_http(server, hc) if hc else _probe(server)
    with _lock:
        prev = _state.get(key)
        # `since` only resets when the up/down state actually flips.
        since = prev["since"] if (prev and prev["up"] == up) else _now_iso()
        rec = {
            "server": server,
            "up": up,
            "since": since,
            "checked": _now_iso(),
            "latency_ms": latency,
            "error": error,
            "_mono": now,
        }
        _state[key] = rec
        return {k: v for k, v in rec.items() if not k.startswith("_")}


def _mapping_servers(mapping):
    """[(server, enabled_bool)] for a mapping, supporting legacy `backend`."""
    pool = mapping.get("backends")
    if not pool:
        b = mapping.get("backend")
        pool = [{"server": b}] if b else []
    out = []
    for b in pool:
        if isinstance(b, str):
            out.append((b, True))
        elif isinstance(b, dict) and b.get("server"):
            out.append((b["server"], not b.get("down")))
    return out


def _rollup(results):
    """results: [{up, enabled}] -> 'green' | 'yellow' | 'red' | 'gray'."""
    enabled = [r for r in results if r["enabled"]]
    if not enabled:
        return "gray"
    up = sum(1 for r in enabled if r["up"])
    if up == 0:
        return "red"
    if up == len(enabled):
        return "green"
    return "yellow"


def check_all(force=False):
    """
    Probe every backend of every mapping (concurrently, deduped) and return:

        {
          "checked": "<iso>",
          "mappings": { domain: {status, up, total, backends: [...] } },
          "summary":  {green, yellow, red, gray, total}
        }
    """
    mappings = storage.list_mappings()

    # Build probe jobs keyed by (server + health-check spec) so a host shared by
    # several mappings is probed once — unless they use different health checks.
    jobs = {}   # cache_key -> (server, hc)
    for m in mappings:
        hc = hc_spec(m)
        for s, _ in _mapping_servers(m):
            jobs.setdefault(_cache_key(s, hc), (s, hc))
    probed = {}
    if jobs:
        keys = list(jobs)
        pool = ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(keys)))
        futures = {}
        with _lock:
            for k in keys:
                fut = _inflight.get(k)
                if fut is None or fut.done():
                    server, hc = jobs[k]
                    fut = pool.submit(_check, server, force, hc)
                    _inflight[k] = fut
                futures[fut] = k
        done, pending = _wait_futures(futures, timeout=CHECK_ALL_DEADLINE)
        for fut in done:
            probed[futures[fut]] = fut.result()
        for fut in pending:
            # Blew the deadline (see CHECK_ALL_DEADLINE) — don't make the
            # whole request wait on it. Fall back to whatever the cache last
            # had for this job (probably will have settled by the next poll,
            # since the probe keeps running in the background) rather than
            # stalling the response for everyone else's already-known state.
            k = futures[fut]
            server, hc = jobs[k]
            with _lock:
                prev = _state.get(_cache_key(server, hc))
            probed[k] = ({kk: vv for kk, vv in prev.items() if not kk.startswith("_")}
                         if prev else {"server": server, "up": None, "since": None,
                                       "checked": None, "latency_ms": None, "error": "probe pending"})
        # Not pool.shutdown() via `with` (which blocks until every submitted
        # task finishes) — let any still-running probe finish on its own time
        # in the background so it's ready in cache for the next poll, instead
        # of holding this response hostage to it.
        pool.shutdown(wait=False)

    out_mappings = {}
    summary = {"green": 0, "yellow": 0, "red": 0, "gray": 0, "total": 0}
    for m in mappings:
        # A TCP connect probe says nothing about a UDP service (UDP has no
        # handshake), so report UDP backends as "not probed" rather than down.
        is_udp = (m.get("transport") or "tcp").lower() == "udp"
        hc = hc_spec(m)
        # Failover: per-backend priority tier + which tier is currently active.
        is_failover = bool(m.get("failover")) and not is_udp
        prio_of, active_p = {}, None
        if is_failover:
            for b in (m.get("backends") or []):
                if isinstance(b, dict) and b.get("server"):
                    prio_of[b["server"]] = int(b.get("priority") or 1)
            tiers = set(prio_of.values()) or {1}
            ap = m.get("active_priority")
            active_p = ap if ap in tiers else min(tiers)
        rows = []
        for srv, enabled in _mapping_servers(m):
            if is_udp:
                rows.append({"server": srv, "up": None, "since": None,
                             "error": "udp (not probed)", "enabled": enabled})
                continue
            st = probed.get(_cache_key(srv, hc), {"server": srv, "up": False, "error": "unknown"})
            row = {**st, "enabled": enabled}
            if is_failover:
                row["priority"] = prio_of.get(srv, 1)
                row["active"] = (row["priority"] == active_p)
            rows.append(row)
        status = "gray" if is_udp else _rollup(rows)
        up = sum(1 for r in rows if r["enabled"] and r["up"])
        total = sum(1 for r in rows if r["enabled"])
        # Keyed by domain:port — a domain may map on several ports, each its own row.
        key = f"{m['domain']}:{m.get('listen_port') or config.LISTEN_PORT}"
        out_mappings[key] = {
            "status": status,
            "up": up,
            "total": total,
            "failover": is_failover,
            "active_priority": active_p,
            "backends": rows,
        }
        summary[status] += 1
        summary["total"] += 1

    return {"checked": _now_iso(), "mappings": out_mappings, "summary": summary}
