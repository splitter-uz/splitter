"""
Docker backend reconciler.

Mappings can reference a backend by Docker container name instead of a static
address (see docker_detect). A container's IP changes when it is recreated, so a
background loop re-resolves every docker-backed backend and, when an address has
changed, re-renders the mapping's nginx config and reloads — keeping the proxy
pointed at the live container without any user action. Same self-healing pattern
as failover.py / the access-list refresher.
"""
import threading
import time

import activity
import config
import docker_detect
import health
import nginx_manager as nm
import storage

INTERVAL = 8          # seconds between reconciliation passes
_scheduler_started = False
# Serialize reconcile: the poll thread and the events watcher both call
# run_once(); only one may rewrite configs + reload nginx at a time. Non-blocking
# so a concurrent caller skips rather than piling up — the change it cared about
# is re-picked-up by the next event/poll wake.
_run_lock = threading.Lock()


def _reconcile_mapping(m):
    """Keep this mapping's docker backends current AND drop unreachable ones from
    the load-balancing pool so clients aren't proxied onto a dead backend (502).

    - Always: re-resolve each container's IP (it changes on restart).
    - Load-balanced pool (non-failover, non-UDP): probe each backend and mark
      unreachable ones `down` (dropped from the upstream), un-parking them when
      they recover. As a safety net we never drop the LAST backend — if every
      backend looks unhealthy (e.g. a misconfigured health check) we leave the
      pool intact rather than blackhole it.
    - Failover mappings are left to failover.py (it promotes tiers by health);
      here we only park a backend whose container has vanished entirely.

    `_docker_parked` marks a backend WE parked, so a user-disabled backend is
    never silently revived. Returns True if anything changed.
    """
    backs = [b for b in (m.get("backends") or []) if b.get("docker_container")]
    if not backs:
        return False
    is_failover = bool(m.get("failover"))
    is_udp = (m.get("transport") or "tcp").lower() == "udp"
    hc = health.hc_spec(m)
    changed = False

    # 1) Keep every container's resolved IP:port current.
    resolved = {}
    for i, b in enumerate(backs):
        r = docker_detect.resolve(b.get("docker_container"), b.get("docker_port"))
        resolved[i] = r
        if r and r != b.get("server"):
            b["server"] = r
            changed = True

    # 2a) Failover / UDP: only handle a container that has disappeared.
    if is_failover or is_udp:
        for i, b in enumerate(backs):
            if resolved[i] is None and not b.get("down"):
                b["down"] = True
                b["_docker_parked"] = True
                changed = True
            elif resolved[i] is not None and b.get("_docker_parked"):
                b.pop("down", None)
                b.pop("_docker_parked", None)
                changed = True
        return changed

    # 2b) Load-balanced pool: probe reachability (TCP connect, or the mapping's
    # HTTP health check) and drop/restore backends accordingly.
    healthy = {}
    for i, b in enumerate(backs):
        healthy[i] = bool(resolved[i]) and bool(
            health._check(resolved[i], force=True, hc=hc).get("up"))
    any_healthy = any(healthy.values())
    for i, b in enumerate(backs):
        if not healthy[i] and not b.get("down") and any_healthy:
            b["down"] = True                 # drop from the pool
            b["_docker_parked"] = True
            changed = True
        elif healthy[i] and b.get("down") and b.get("_docker_parked"):
            b.pop("down", None)              # back in the pool
            b.pop("_docker_parked", None)
            changed = True
    return changed


def run_once():
    """One reconciliation pass over all enabled, docker-backed mappings. Rewrites
    the configs that changed and reloads nginx ONCE at the end (not per mapping),
    so a burst — e.g. a Swarm rollout — is a single reload. Serialized by
    _run_lock; a concurrent caller skips (the wake that mattered re-fires)."""
    if not _run_lock.acquire(blocking=False):
        return
    try:
        changed = []
        for m in storage.list_mappings():
            if not m.get("enabled", True):
                continue
            if not any(b.get("docker_container") for b in (m.get("backends") or [])):
                continue
            fresh = storage.get(m["domain"], m.get("listen_port"))
            if not fresh:
                continue
            fresh = dict(fresh)
            if not _reconcile_mapping(fresh):
                continue
            try:
                if fresh.get("waf_bound"):
                    nm.write_http_conf(fresh)
                else:
                    nm.write_conf(fresh)
                storage.upsert(fresh)
                changed.append(fresh["domain"])
            except Exception:
                pass
        if changed:
            try:
                if nm.test_config()["ok"]:
                    nm.reload_nginx()
            except Exception:
                pass
            activity.record("system", "system", "docker.reconcile",
                            target=", ".join(changed[:5]) + (" …" if len(changed) > 5 else ""),
                            detail="docker backends reconciled (address / pool membership)")
    finally:
        _run_lock.release()


def start_scheduler():
    """Launch the daemon thread that reconciles docker backends (idempotent)."""
    global _scheduler_started
    if _scheduler_started or config.SIMULATE:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                if docker_detect.available():
                    run_once()
            except Exception:
                pass
            time.sleep(INTERVAL)

    threading.Thread(target=_loop, name="docker-reconciler", daemon=True).start()
