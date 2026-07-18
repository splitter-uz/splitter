"""Input validation and sanitisation helpers."""
import ipaddress
import random
import re

# RFC-1123 hostname: labels of [a-z0-9-], not starting/ending with '-'.
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)

# Linux interface names: max 15 chars, restricted charset.
_IFACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")

# Access-list name: also used as a filename (<name>.conf), so keep it to a safe
# lower-case slug that can't escape the acl.d directory.
_ACL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# Names a user list may NOT take (collide with managed/special snippets).
_ACL_RESERVED = {"_default"}

# 6 colon-separated hex octets.
_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def random_mac():
    """
    Generate a random *locally administered, unicast* MAC address.

    The first octet has the locally-administered bit (0x02) set and the
    multicast bit (0x01) cleared, so it never collides with a real vendor OUI.
    """
    first = (random.randint(0, 255) & 0xFC) | 0x02
    octets = [first] + [random.randint(0, 255) for _ in range(5)]
    return ":".join(f"{o:02x}" for o in octets)


def clean_mac(value, allow_blank=True):
    """Validate a MAC; if blank, generate a random locally-administered one."""
    value = (value or "").strip().lower()
    if not value:
        if allow_blank:
            return random_mac()
        raise ValidationError("MAC address is required.")
    if not _MAC_RE.match(value):
        raise ValidationError(f"Invalid MAC address: {value!r}")
    first = int(value.split(":")[0], 16)
    if first & 0x01:
        raise ValidationError("MAC address must be unicast (LSB of first octet = 0).")
    return value


def clean_vlan_id(value):
    """Validate an optional 802.1Q VLAN ID (1–4094). Blank => None."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        vid = int(value)
    except ValueError:
        raise ValidationError(f"Invalid VLAN ID: {value!r}")
    if not (1 <= vid <= 4094):
        raise ValidationError("VLAN ID must be between 1 and 4094.")
    return vid


class ValidationError(ValueError):
    """Raised when user supplied data is rejected."""


def clean_domain(value):
    value = (value or "").strip().lower().rstrip(".")
    if not value or not _DOMAIN_RE.match(value):
        raise ValidationError(f"Invalid domain name: {value!r}")
    # Defence in depth: the domain becomes part of file paths.
    if "/" in value or ".." in value:
        raise ValidationError(f"Illegal characters in domain: {value!r}")
    return value


def clean_ip(value, field="IP address"):
    value = (value or "").strip()
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        raise ValidationError(f"Invalid {field}: {value!r}")
    return str(ip)


def clean_backend(value):
    """Validate a 'host:port' backend target."""
    value = (value or "").strip()
    if ":" not in value:
        raise ValidationError(f"Backend must be host:port, got {value!r}")
    host, _, port = value.rpartition(":")
    host = host.strip()
    # host may be an IP or a hostname.
    try:
        clean_ip(host)
    except ValidationError:
        try:
            host = clean_domain(host)
        except ValidationError:
            raise ValidationError(f"Invalid backend host: {host!r}")
    try:
        port_i = int(port)
    except ValueError:
        raise ValidationError(f"Invalid backend port: {port!r}")
    if not (1 <= port_i <= 65535):
        raise ValidationError(f"Backend port out of range: {port_i}")
    return f"{host}:{port_i}"


def clean_backends(values):
    """Validate a list of 'host:port' backends; dedupe, preserve order."""
    out = []
    for v in values or []:
        v = (v or "").strip()
        if not v:
            continue
        out.append(clean_backend(v))
    out = list(dict.fromkeys(out))  # de-duplicate, keep order
    if not out:
        raise ValidationError("At least one backend server is required.")
    return out


# nginx time values: a number with an optional unit (ms, s, m, h, d, w).
_TIME_RE = re.compile(r"^\d+(ms|s|m|h|d|w)?$")
# nginx size/rate values: bytes/sec with an optional k/m/g suffix (e.g. 1m, 512k).
_RATE_RE = re.compile(r"^\d+[kKmMgG]?$")
# hash key: nginx variables / literals — letters, digits and $ _ . - : { }
_HASHKEY_RE = re.compile(r"^[A-Za-z0-9_$.\-:{}]+$")


def clean_uint(value, field, lo=0, hi=2_000_000):
    value = str(value if value is not None else "").strip()
    if value == "":
        return None
    try:
        n = int(value)
    except ValueError:
        raise ValidationError(f"{field} must be a whole number, got {value!r}")
    if not (lo <= n <= hi):
        raise ValidationError(f"{field} out of range: {n}")
    return n


def clean_time(value, field):
    value = str(value if value is not None else "").strip()
    if value == "":
        return None
    if not _TIME_RE.match(value):
        raise ValidationError(f"{field} must be an nginx time (e.g. 10s, 5m), got {value!r}")
    return value


def clean_rate(value, field):
    """Validate an nginx per-connection rate (bytes/sec, e.g. 1m, 512k). Blank
    or 0 => None (no limit)."""
    value = str(value if value is not None else "").strip().lower()
    if value in ("", "0"):
        return None
    if not _RATE_RE.match(value):
        raise ValidationError(f"{field} must be a byte rate like 1m or 512k, got {value!r}")
    return value


def clean_hash_key(value):
    value = (value or "").strip() or "$remote_addr"
    if not _HASHKEY_RE.match(value):
        raise ValidationError(f"Invalid hash key: {value!r}")
    return value


def clean_backend_pool(items):
    """
    Validate a backend pool. `items` may be a list of 'host:port' strings or a
    list of dicts with optional per-server params. Returns normalized dicts.
    """
    out, seen = [], set()
    for it in items or []:
        if isinstance(it, str):
            it = {"server": it}
        elif not isinstance(it, dict):
            raise ValidationError("Invalid backend entry.")
        raw = (it.get("server") or "").strip()
        if not raw:
            continue
        server = clean_backend(raw)
        if server in seen:
            continue
        seen.add(server)
        entry = {"server": server}
        w = clean_uint(it.get("weight"), "weight", lo=1, hi=1_000_000)
        if w is not None:
            entry["weight"] = w
        # Failover priority tier (1 = highest / tried first). Same priority =
        # active-active within that tier. Only meaningful when failover is on.
        pr = clean_uint(it.get("priority"), "priority", lo=1, hi=999)
        if pr is not None:
            entry["priority"] = pr
        mf = clean_uint(it.get("max_fails"), "max_fails")
        if mf is not None:
            entry["max_fails"] = mf
        ft = clean_time(it.get("fail_timeout"), "fail_timeout")
        if ft:
            entry["fail_timeout"] = ft
        mc = clean_uint(it.get("max_conns"), "max_conns", lo=1)
        if mc is not None:
            entry["max_conns"] = mc
        if it.get("backup"):
            entry["backup"] = True
        if it.get("down"):
            entry["down"] = True
        # Docker-discovered backend: remember which container/port this server
        # was resolved from so the reconciler can keep its IP current. The
        # `server` field has already been resolved to a concrete IP:PORT by the
        # caller (app._parse_lb); these are just provenance.
        dc = (it.get("docker_container") or "").strip()
        if dc:
            entry["docker_container"] = dc
            dp = clean_uint(it.get("docker_port"), "docker port", lo=1, hi=65535)
            if dp is not None:
                entry["docker_port"] = dp
        out.append(entry)
    if not out:
        raise ValidationError("At least one backend server is required.")
    return out


def clean_interface(value, allowed=None):
    value = (value or "").strip()
    if not value or not _IFACE_RE.match(value):
        raise ValidationError(f"Invalid interface name: {value!r}")
    if allowed is not None and value not in allowed:
        raise ValidationError(f"Unknown interface: {value!r}")
    return value


def clean_port(value, default, field="Listen port"):
    """Validate a TCP port (1–65535). Blank falls back to `default`."""
    value = str(value if value is not None else "").strip() or str(default)
    try:
        p = int(value)
    except ValueError:
        raise ValidationError(f"Invalid {field.lower()}: {value!r}")
    if not (1 <= p <= 65535):
        raise ValidationError(f"{field} out of range: {p}")
    return p


def clean_transport(value, default="tcp"):
    """Validate the listener transport: 'tcp' or 'udp'."""
    value = (value or "").strip().lower() or default
    if value not in ("tcp", "udp"):
        raise ValidationError(f"Transport must be tcp or udp, got {value!r}")
    return value


def clean_acl_name(value):
    """Validate an access-list name (becomes the <name>.conf filename)."""
    value = (value or "").strip().lower()
    if not value or not _ACL_NAME_RE.match(value):
        raise ValidationError(
            f"Invalid access-list name {value!r} — use lower-case letters, "
            "digits, '-' or '_' (max 40 chars, must start alphanumeric).")
    if value in _ACL_RESERVED:
        raise ValidationError(f"{value!r} is a reserved name.")
    return value


def clean_cidr(value, field="network"):
    """Validate a single IPv4/IPv6 CIDR (or bare host address). Returns the
    normalized 'network/prefix' string."""
    value = (value or "").strip()
    try:
        # strict=False tolerates host bits set (e.g. 10.0.0.5/24 -> 10.0.0.0/24).
        net = ipaddress.ip_network(value, strict=False)
    except ValueError:
        raise ValidationError(f"Invalid {field}: {value!r}")
    return str(net)


def clean_cidr_list(text):
    """Parse a textarea/list of CIDRs (one per line, or comma/space separated).
    Ignores blank lines and '#' comments (whole-line or inline). Dedupes,
    preserves order."""
    if isinstance(text, (list, tuple)):
        lines = list(text)
    else:
        lines = (text or "").splitlines()
    out = []
    for line in lines:
        line = (line or "").split("#", 1)[0]  # strip inline/whole-line comments
        for tok in re.split(r"[\s,]+", line.strip()):
            if tok:
                out.append(clean_cidr(tok))
    return list(dict.fromkeys(out))  # de-duplicate, keep order


def clean_prefix(value, default):
    value = (value or "").strip() or str(default)
    try:
        p = int(value)
    except ValueError:
        raise ValidationError(f"Invalid CIDR prefix: {value!r}")
    if not (0 <= p <= 32):
        raise ValidationError(f"CIDR prefix out of range: {p}")
    return str(p)


# --- Firewall (per-interface iptables rules) -------------------------------
_FW_PROTOCOLS = ("tcp", "udp", "icmp", "all")
_FW_ACTIONS = ("accept", "drop", "reject")
_FW_DIRECTIONS = ("in", "out")


def clean_fw_protocol(value):
    value = (value or "tcp").strip().lower()
    if value not in _FW_PROTOCOLS:
        raise ValidationError(f"Protocol must be one of {', '.join(_FW_PROTOCOLS)}, got {value!r}")
    return value


def clean_fw_action(value):
    value = (value or "accept").strip().lower()
    if value not in _FW_ACTIONS:
        raise ValidationError(f"Action must be one of {', '.join(_FW_ACTIONS)}, got {value!r}")
    return value


def clean_fw_direction(value):
    value = (value or "in").strip().lower()
    if value not in _FW_DIRECTIONS:
        raise ValidationError(f"Direction must be 'in' or 'out', got {value!r}")
    return value


def clean_port_range(value, field="Port"):
    """Validate a single port or 'low-high' range for a firewall rule. Blank
    means "any port" (only meaningful for tcp/udp)."""
    value = (value or "").strip()
    if not value:
        return None
    parts = value.split("-")
    if len(parts) not in (1, 2):
        raise ValidationError(f"Invalid {field.lower()}: {value!r}")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValidationError(f"Invalid {field.lower()}: {value!r}")
    for n in nums:
        if not (1 <= n <= 65535):
            raise ValidationError(f"{field} out of range: {n}")
    if len(nums) == 2 and nums[0] > nums[1]:
        raise ValidationError(f"{field} range must be low-high, got {value!r}")
    return "-".join(str(n) for n in nums)


def clean_fw_source(value):
    """Validate a firewall rule's source CIDR. Blank means "anywhere"."""
    value = (value or "").strip()
    if not value:
        return "0.0.0.0/0"
    return clean_cidr(value, field="source")
