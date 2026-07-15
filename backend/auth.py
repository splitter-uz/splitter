"""
User accounts & authentication for Splitter.

Portainer-style bootstrap: on first run there are no users, so the UI shows a
one-time "create the initial admin" screen. After that, login is required.

Users are stored as JSON next to the mappings (in config.DATA_DIR) so they
survive a redeploy. Passwords are stored only as salted Werkzeug hashes.

Roles:
    admin   — full control, including managing users and destructive/bulk
              actions (delete mapping, import, re-apply all).
    creator — may add and edit mappings and export; may NOT delete mappings,
              import, re-apply, or manage users.
"""
import json
import os
import re
import tempfile
import threading

from werkzeug.security import check_password_hash, generate_password_hash

import config

ROLES = ("admin", "creator")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD_LEN = 8
# pbkdf2 is available everywhere; Werkzeug's newer scrypt default needs an
# OpenSSL build that some Python installs lack.
_HASH_METHOD = "pbkdf2:sha256"

_lock = threading.RLock()
USERS_FILE = os.path.join(config.DATA_DIR, "users.json")


class AuthError(Exception):
    """Raised for invalid user-management input (bad username/role/password)."""


def _read():
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write(data):
    os.makedirs(config.DATA_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=config.DATA_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, USERS_FILE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _public(username, rec):
    return {"username": username, "role": rec["role"]}


def any_users():
    """True once at least one account exists (i.e. setup is done)."""
    with _lock:
        return bool(_read())


def list_users():
    with _lock:
        return [_public(u, r) for u, r in sorted(_read().items())]


def get_user(username):
    with _lock:
        rec = _read().get(username)
        return _public(username, rec) if rec else None


def _validate(username, password, role):
    if not _USERNAME_RE.match(username or ""):
        raise AuthError("Username must be 3–32 characters: letters, digits, _ . -")
    if role not in ROLES:
        raise AuthError("Role must be 'admin' or 'creator'.")
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")


def create_user(username, password, role):
    username = (username or "").strip()
    _validate(username, password, role)
    with _lock:
        data = _read()
        if username in data:
            raise AuthError(f"User {username!r} already exists.")
        data[username] = {"role": role, "password": generate_password_hash(password, method=_HASH_METHOD)}
        _write(data)
    return _public(username, data[username])


def verify(username, password):
    """Return the public user dict on a correct username+password, else None."""
    with _lock:
        rec = _read().get((username or "").strip())
    if rec and check_password_hash(rec["password"], password or ""):
        return _public(username.strip(), rec)
    return None


def set_password(username, password):
    if not password or len(password) < MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LEN} characters.")
    with _lock:
        data = _read()
        if username not in data:
            return False
        data[username]["password"] = generate_password_hash(password, method=_HASH_METHOD)
        _write(data)
        return True


def set_role(username, role):
    if role not in ROLES:
        raise AuthError("Role must be 'admin' or 'creator'.")
    with _lock:
        data = _read()
        if username not in data:
            return False
        # Don't allow demoting the only remaining admin — it would lock
        # everyone out of user management.
        if data[username]["role"] == "admin" and role != "admin":
            admins = [u for u, r in data.items() if r["role"] == "admin"]
            if len(admins) <= 1:
                raise AuthError("Cannot demote the last admin account.")
        data[username]["role"] = role
        _write(data)
        return True


def delete_user(username):
    with _lock:
        data = _read()
        if username not in data:
            return False
        if data[username]["role"] == "admin":
            admins = [u for u, r in data.items() if r["role"] == "admin"]
            if len(admins) <= 1:
                raise AuthError("Cannot delete the last admin account.")
        del data[username]
        _write(data)
        return True
