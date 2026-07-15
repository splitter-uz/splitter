"""
Append-only activity / audit log for Splitter.

Records who did what and when — logins, mapping create/update/delete, re-apply,
import/export and user-management actions — so admins can review account
activity from the UI's Activity page.

Persisted as JSON next to the mappings/users store (config.DATA_DIR) so it
survives a redeploy, and capped to the most recent MAX_EVENTS entries so the
file can't grow without bound. Recording is best-effort: an audit failure must
never break the action being audited.
"""
import datetime
import json
import os
import tempfile
import threading

import config

_lock = threading.RLock()
LOG_FILE = os.path.join(config.DATA_DIR, "activity.json")
MAX_EVENTS = 2000


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read():
    try:
        with open(LOG_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write(events):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(events, fh, indent=2)
        os.replace(tmp, LOG_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def record(user, role, action, target=None, detail=None, ip=None):
    """Append one audit entry. Never raises."""
    entry = {
        "ts": _now(),
        "user": user or "—",
        "role": role or "—",
        "action": action,
        "target": target,
        "detail": detail,
        "ip": ip,
    }
    try:
        with _lock:
            events = _read()
            events.append(entry)
            if len(events) > MAX_EVENTS:
                events = events[-MAX_EVENTS:]
            _write(events)
    except Exception:
        pass
    return entry


def list_events(limit=200):
    """Most-recent-first list of audit entries (capped at `limit`, 0 = all)."""
    with _lock:
        events = _read()
    events.reverse()
    return events[:limit] if limit else events
