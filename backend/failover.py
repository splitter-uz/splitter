"""
Active-passive priority failover orchestration.

nginx OSS has only *passive* health checks and just two upstream tiers (normal +
`backup`). Splitter already actively TCP-probes backends (health.py), so we layer
**active, N-level priority failover** on top: a background loop probes each
failover mapping's backends and keeps traffic on the lowest-priority tier that
still has a healthy backend, re-rendering the mapping's nginx config (marking the
other tiers `down`) and reloading on any change.

Flap protection: failover (to a lower-priority tier) happens immediately when the
active tier loses all healthy backends; fail-BACK (to a recovered higher-priority
tier) waits for a few consecutive healthy cycles so a flapping primary doesn't
thrash traffic.
"""
import threading
import time

import activity
import config
import health
import nginx_manager as nm
import storage


def _mkey(m):
    """Composite mapping identity (domain:port) used for per-mapping state."""
    return f"{m['domain']}:{m.get('listen_port') or config.LISTEN_PORT}"

INTERVAL = 5                # seconds between failover evaluations
FAILBACK_STABLE_CYCLES = 3  # consecutive healthy cycles before failing back

_failback_streak = {}       # domain -> consecutive cycles a better tier was healthy
_scheduler_started = False


def desired_active_priority(mapping, up_map):
    """Lowest priority tier with >=1 enabled, healthy backend; None if all down.

    `up_map`: {server: bool}. Servers missing or False are treated as down.
    """
    tiers = {}
    for b in nm._normalize_backends(mapping):
        if b.get("down"):                       # user-disabled backend
            continue
        tiers.setdefault(nm._effective_priority(b), []).append(b["server"])
    for p in sorted(tiers):
        if any(up_map.get(s) for s in tiers[p]):
            return p
    return None


def evaluate(mapping, up_map):
    """New active_priority for this mapping, or None to keep the current one.

    Immediate failover (active tier down), delayed fail-back (flap protection).
    """
    domain = _mkey(mapping)
    current = nm.failover_active_priority(mapping)
    desired = desired_active_priority(mapping, up_map)

    if desired is None or desired == current:
        _failback_streak.pop(domain, None)
        return None
    if desired > current:
        # The active (higher-priority) tier has no healthy backend — fail over now.
        _failback_streak.pop(domain, None)
        return desired
    # desired < current: a higher-priority tier recovered — fail back, but only
    # after it has stayed healthy for a few consecutive cycles.
    streak = _failback_streak.get(domain, 0) + 1
    _failback_streak[domain] = streak
    if streak >= FAILBACK_STABLE_CYCLES:
        _failback_streak.pop(domain, None)
        return desired
    return None


def _apply(m, new_priority):
    """Persist the new active tier, re-render the mapping's config and reload."""
    domain = m["domain"]
    fresh = storage.get(domain, m.get("listen_port"))
    if not fresh:
        return
    old = nm.failover_active_priority(fresh)
    fresh = dict(fresh)
    fresh["active_priority"] = new_priority
    # Render via the correct path (L4 stream, or L7 when WAF-bound).
    if fresh.get("waf_bound"):
        nm.write_http_conf(fresh)
    else:
        nm.write_conf(fresh)
    if nm.test_config()["ok"]:
        nm.reload_nginx()
    storage.upsert(fresh)
    direction = "failback" if new_priority < old else "failover"
    activity.record("system", "system", f"failover.{direction}",
                    target=domain, detail=f"priority {old} -> {new_priority}")


def run_once():
    """One evaluation pass over all enabled failover mappings."""
    for m in storage.list_mappings():
        if not m.get("failover") or not m.get("enabled", True):
            continue
        # UDP has no TCP health signal, so active failover can't apply to it.
        if (m.get("transport") or "tcp").lower() == "udp":
            continue
        servers = [b["server"] for b in nm._normalize_backends(m) if not b.get("down")]
        hc = health.hc_spec(m)   # HTTP health-check if configured, else TCP connect
        up_map = {s: bool(health._check(s, force=True, hc=hc).get("up")) for s in servers}
        new_p = evaluate(m, up_map)
        if new_p is not None:
            try:
                _apply(m, new_p)
            except Exception:
                pass


def start_scheduler():
    """Launch the daemon thread that drives failover (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                run_once()
            except Exception:
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=_loop, name="failover-scheduler", daemon=True).start()
