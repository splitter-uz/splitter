"""
Full-system backup & restore for Splitter.

A backup is a single **zip** bundling every JSON data store in `config.DATA_DIR`
(mappings, users, certs, sub-interfaces, settings, VLAN registry, activity log)
plus the managed SSL cert/key files in `config.SSL_DIR`, with a `manifest.json`
describing it. Backups are timestamp-named so you can restore the system to a
chosen point in time.

Snapshots are saved under `DATA_DIR/backups/`. A background scheduler can take
automatic snapshots on an interval and prune to a retention count. Everything is
SIMULATE-agnostic — it only touches the tool's own data files, never the host.

SECURITY: a backup contains password hashes and SSL **private keys**. The API
keeps every backup route admin-only; treat the downloaded file as a secret.
"""
import datetime
import glob
import io
import json
import os
import re
import tempfile
import threading
import time
import zipfile

import config
import storage

BACKUPS_DIR = os.path.join(config.DATA_DIR, "backups")
_NAME_RE = re.compile(r"^splitter-backup-[0-9A-Za-z._-]+\.zip$")
_LABEL_RE = re.compile(r"[^0-9A-Za-z._-]+")
_lock = threading.RLock()


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M%S")


def _ensure_dir():
    os.makedirs(BACKUPS_DIR, exist_ok=True)


def _data_json_files():
    """Top-level *.json stores in DATA_DIR (never the backups/ subdir)."""
    return sorted(p for p in glob.glob(os.path.join(config.DATA_DIR, "*.json"))
                  if os.path.isfile(p))


def _ssl_files():
    if not os.path.isdir(config.SSL_DIR):
        return []
    out = []
    for ext in ("*.crt", "*.key", "*.pem"):
        out.extend(glob.glob(os.path.join(config.SSL_DIR, ext)))
    return sorted(p for p in out if os.path.isfile(p))


def _count_json(fname):
    try:
        with open(os.path.join(config.DATA_DIR, fname), encoding="utf-8") as fh:
            data = json.load(fh)
        return len(data) if isinstance(data, (list, dict)) else 0
    except Exception:
        return 0


def _build_manifest(label, auto):
    return {
        "splitter_backup": True,
        "type": "full",
        "version": 1,
        "created": _now_iso(),
        "auto": bool(auto),
        "label": label or "",
        "counts": {
            "mappings": _count_json("mappings.json"),
            "users": _count_json("users.json"),
            "certs": _count_json("certs.json"),
            "subinterfaces": _count_json("subinterfaces.json"),
            "ssl_files": len(_ssl_files()),
        },
    }


def _write_zip(fileobj, label, auto):
    manifest = _build_manifest(label, auto)
    with zipfile.ZipFile(fileobj, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for path in _data_json_files():
            zf.write(path, arcname="data/" + os.path.basename(path))
        for path in _ssl_files():
            zf.write(path, arcname="ssl/" + os.path.basename(path))
    return manifest


# --------------------------------------------------------------------------
# Create
# --------------------------------------------------------------------------
def snapshot_bytes(label=None):
    """A fresh full backup as raw zip bytes (for a direct download, not stored)."""
    buf = io.BytesIO()
    with _lock:
        _write_zip(buf, label, auto=False)
    return buf.getvalue()


def store_bytes(raw, label=None):
    """Persist already-built backup bytes (e.g. an uploaded archive) into
    BACKUPS_DIR under a fresh timestamped name. Returns its metadata."""
    with _lock:
        _ensure_dir()
        label = _LABEL_RE.sub("-", (label or "").strip()).strip("-")[:40]
        suffix = f"-{label}" if label else ""
        name = f"splitter-backup-{_stamp()}{suffix}.zip"
        path = os.path.join(BACKUPS_DIR, name)
        i = 1
        while os.path.exists(path):
            name = f"splitter-backup-{_stamp()}{suffix}-{i}.zip"
            path = os.path.join(BACKUPS_DIR, name)
            i += 1
        with open(path, "wb") as fh:
            fh.write(raw)
        return meta_of(path)


def create(label=None, auto=False):
    """Write a timestamped backup into BACKUPS_DIR. Returns its metadata."""
    with _lock:
        _ensure_dir()
        label = _LABEL_RE.sub("-", (label or "").strip()).strip("-")[:40]
        suffix = f"-{label}" if label else ("-auto" if auto else "")
        name = f"splitter-backup-{_stamp()}{suffix}.zip"
        path = os.path.join(BACKUPS_DIR, name)
        # Avoid clobbering if two land in the same second.
        i = 1
        while os.path.exists(path):
            name = f"splitter-backup-{_stamp()}{suffix}-{i}.zip"
            path = os.path.join(BACKUPS_DIR, name)
            i += 1
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            _write_zip(fh, label, auto)
        os.replace(tmp, path)
        return meta_of(path)


# --------------------------------------------------------------------------
# List / inspect
# --------------------------------------------------------------------------
def meta_of(path):
    name = os.path.basename(path)
    st = os.stat(path)
    manifest = {}
    try:
        with zipfile.ZipFile(path) as zf:
            with zf.open("manifest.json") as fh:
                manifest = json.load(fh)
    except Exception:
        pass
    return {
        "name": name,
        "size": st.st_size,
        "created": manifest.get("created"),
        "mtime": datetime.datetime.fromtimestamp(
            st.st_mtime, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "auto": bool(manifest.get("auto")),
        "label": manifest.get("label", ""),
        "counts": manifest.get("counts", {}),
        "valid": bool(manifest.get("splitter_backup")),
    }


def list_backups():
    _ensure_dir()
    items = [meta_of(p) for p in glob.glob(os.path.join(BACKUPS_DIR, "*.zip"))]
    # Newest first, by created timestamp then mtime.
    items.sort(key=lambda m: (m.get("created") or m.get("mtime") or ""), reverse=True)
    return items


def resolve(name):
    """Validate a user-supplied backup name and return its absolute path, or None."""
    if not name or not _NAME_RE.match(name) or "/" in name or "\\" in name:
        return None
    path = os.path.join(BACKUPS_DIR, name)
    return path if os.path.isfile(path) else None


def path_bytes(name):
    path = resolve(name)
    if not path:
        return None
    with open(path, "rb") as fh:
        return fh.read()


def delete(name):
    path = resolve(name)
    if not path:
        return False
    os.remove(path)
    return True


# --------------------------------------------------------------------------
# Restore
# --------------------------------------------------------------------------
def _safe_member(arc):
    """Reject path traversal; only allow data/<f>.json and ssl/<f>."""
    if arc.startswith("data/") and arc.endswith(".json"):
        sub = arc[len("data/"):]
    elif arc.startswith("ssl/"):
        sub = arc[len("ssl/"):]
    else:
        return None
    if not sub or "/" in sub or "\\" in sub or sub.startswith("."):
        return None
    return arc


def restore_bytes(raw):
    """Restore the system from raw zip bytes. Returns a result dict. Atomic per
    file (temp + os.replace); SSL private keys are written 0600."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ValueError("Not a valid backup archive (zip expected).")
    with zf:
        try:
            manifest = json.loads(zf.read("manifest.json"))
        except KeyError:
            raise ValueError("Archive has no manifest — not a Splitter backup.")
        if not manifest.get("splitter_backup"):
            raise ValueError("Archive is not a Splitter backup.")

        restored = {"data": [], "ssl": []}
        with _lock:
            os.makedirs(config.DATA_DIR, exist_ok=True)
            for info in zf.infolist():
                arc = _safe_member(info.filename)
                if not arc:
                    continue
                payload = zf.read(info.filename)
                if arc.startswith("data/"):
                    dest_dir, bucket = config.DATA_DIR, "data"
                else:
                    os.makedirs(config.SSL_DIR, exist_ok=True)
                    dest_dir, bucket = config.SSL_DIR, "ssl"
                base = os.path.basename(arc)
                dest = os.path.join(dest_dir, base)
                fd, tmp = tempfile.mkstemp(dir=dest_dir, suffix=".tmp")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                if base.endswith(".key"):
                    os.chmod(tmp, 0o600)
                os.replace(tmp, dest)
                restored[bucket].append(base)
        return {"manifest": manifest, "restored": restored}


def restore_name(name):
    raw = path_bytes(name)
    if raw is None:
        raise ValueError("No such backup.")
    return restore_bytes(raw)


# --------------------------------------------------------------------------
# Scheduler (in-app, no system cron needed)
# --------------------------------------------------------------------------
def schedule_config():
    s = storage.settings_all()
    return {
        "enabled": bool(s.get("backup_enabled", False)),
        "interval_hours": int(s.get("backup_interval_hours", 24) or 24),
        "retention": int(s.get("backup_retention", 7) or 7),
        "last_run": s.get("backup_last_run"),
    }


def prune(retention):
    """Keep the newest `retention` AUTOMATIC backups; never touch manual ones."""
    autos = [m for m in list_backups() if m["auto"]]
    removed = []
    for m in autos[retention:]:
        if delete(m["name"]):
            removed.append(m["name"])
    return removed


def run_scheduled():
    """Take one automatic snapshot and prune. Returns the new meta or None."""
    cfg = schedule_config()
    meta = create(auto=True)
    storage.settings_update({"backup_last_run": _now_iso()})
    prune(cfg["retention"])
    return meta


def _due(cfg):
    if not cfg["enabled"]:
        return False
    last = cfg.get("last_run")
    if not last:
        return True
    try:
        prev = datetime.datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc)
    except (ValueError, TypeError):
        return True
    age_h = (datetime.datetime.now(datetime.timezone.utc) - prev).total_seconds() / 3600
    return age_h >= cfg["interval_hours"]


_scheduler_started = False


def start_scheduler():
    """Launch the daemon thread that takes due automatic backups (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                if _due(schedule_config()):
                    run_scheduled()
            except Exception:
                pass
            time.sleep(60)

    threading.Thread(target=_loop, name="backup-scheduler", daemon=True).start()
