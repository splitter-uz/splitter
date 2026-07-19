"""
Docker events watcher — Traefik-style instant reaction to container lifecycle.

docker_reconcile polls every few seconds; this streams the Docker events API
(GET /events) and fires an *immediate* reconcile the moment a container starts,
stops, dies, or reports a health change (and on Swarm service updates). So a dead
backend is dropped from the load-balanced pool — and a recovered one restored —
within a fraction of a second, without waiting for the poll. It's the same idea
as Traefik's provider watch; nginx stays the data plane, we just keep its
upstream list in step with Docker in real time. The periodic poll remains as a
safety net and reconnect fallback.
"""
import json
import threading
import time
import urllib.parse

import config
import docker_detect
import docker_reconcile

# Container actions that can change a backend's availability.
_ACTIONS = {"start", "die", "stop", "kill", "destroy", "create",
            "pause", "unpause", "health_status", "restart", "update", "rename"}

_started = False
_wake = threading.Event()


def _reconcile_worker():
    """Debounced reconcile: coalesce a burst of events (e.g. a Swarm rollout)
    into a single reconcile pass so we don't rewrite nginx once per event."""
    while True:
        _wake.wait()
        time.sleep(0.3)          # let a burst settle
        _wake.clear()
        try:
            docker_reconcile.run_once()
        except Exception:
            pass


def _stream_events():
    """Block on the Docker events stream, waking the reconcile worker on any
    relevant container/service event. Returns when the stream ends/errors."""
    flt = urllib.parse.quote(json.dumps({"type": ["container", "service"]}))
    conn = docker_detect._UnixHTTPConnection(docker_detect.DOCKER_SOCK, timeout=None)
    try:
        conn.request("GET", f"/events?filters={flt}")
        resp = conn.getresponse()
        if resp.status != 200:
            raise RuntimeError(f"events HTTP {resp.status}")
        for raw in resp:                     # streamed, line-delimited JSON
            line = raw.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            typ = evt.get("Type")
            action = (evt.get("Action") or "").split(":")[0].strip()
            if typ == "service" or (typ == "container" and action in _ACTIONS):
                _wake.set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _watch_loop():
    while True:
        if not docker_detect.available():
            time.sleep(5)
            continue
        try:
            _stream_events()
        except Exception:
            pass
        time.sleep(2)            # reconnect backoff after the stream drops


def start_watcher():
    """Launch the events watcher + debounced reconcile worker (idempotent)."""
    global _started
    if _started or config.SIMULATE:
        return
    _started = True
    threading.Thread(target=_reconcile_worker, name="docker-events-reconcile", daemon=True).start()
    threading.Thread(target=_watch_loop, name="docker-events-watch", daemon=True).start()
