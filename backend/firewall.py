"""
Per-interface firewall — security-group style iptables rules.

Every managed interface gets two custom chains, SFW-IN-<iface> and
SFW-OUT-<iface>, each hooked into INPUT/OUTPUT with a single jump rule
(`-i <iface> -j SFW-IN-<iface>` / `-o <iface> -j SFW-OUT-<iface>`). A chain is
always fully rebuilt (flushed and repopulated) from storage on every apply, so
the JSON rule set is the single source of truth and nothing here is additive
across calls — re-running apply is always safe.

Nothing is touched on the host until BOTH the global "firewall_enabled"
setting and the interface's own "enabled" flag are on, so adding this feature
changes no host behaviour until an admin opts in.

Every managed chain always starts with an ESTABLISHED/RELATED accept (so
existing connections, including the request that's configuring the firewall,
survive a policy change) and every inbound chain always accepts this
dashboard's own port, so a misconfigured rule set can't lock the admin out of
the tool that would fix it. `panic()` is the emergency escape hatch: it tears
down every managed chain/jump and flips the master switch off.
"""
import subprocess

import config
import nginx_manager as nm
import storage

CHAIN_IN_PREFIX = "SFW-IN-"
CHAIN_OUT_PREFIX = "SFW-OUT-"

_TARGET = {"accept": "ACCEPT", "drop": "DROP", "reject": "REJECT"}


def _chain(interface, direction):
    prefix = CHAIN_IN_PREFIX if direction == "in" else CHAIN_OUT_PREFIX
    return f"{prefix}{interface}"


def _hook(direction):
    return "INPUT" if direction == "in" else "OUTPUT"


def _iface_flag(direction):
    return "-i" if direction == "in" else "-o"


def _rule_args(rule):
    args = []
    proto = rule.get("protocol", "tcp")
    if proto != "all":
        args += ["-p", proto]
    if proto in ("tcp", "udp") and rule.get("port_range"):
        args += ["--dport", rule["port_range"]]
    src = rule.get("source_cidr") or "0.0.0.0/0"
    if src != "0.0.0.0/0":
        args += ["-s", src]
    args += ["-j", _TARGET.get(rule.get("action", "accept"), "ACCEPT")]
    return args


def _rule_exists(argv):
    """True if an equivalent iptables rule is already installed (via -C).
    Always False in SIMULATE, since there is no real state to check."""
    if config.SIMULATE:
        return False
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _ensure_chain(chain, steps):
    steps.append(nm.run_cmd(f"ensure chain {chain}",
                             nm._privileged(["iptables", "-N", chain]),
                             ok_returncodes=(0, 1)))  # 1 = chain already exists
    steps.append(nm.run_cmd(f"flush chain {chain}",
                             nm._privileged(["iptables", "-F", chain])))


def _ensure_jump(hook, iface_flag, interface, chain, steps):
    argv = nm._privileged(["iptables", "-C", hook, iface_flag, interface, "-j", chain])
    if _rule_exists(argv):
        return
    steps.append(nm.run_cmd(f"hook {hook} -> {chain}",
                             nm._privileged(["iptables", "-I", hook, "1",
                                              iface_flag, interface, "-j", chain])))


def _remove_jump(hook, iface_flag, interface, chain, steps):
    steps.append(nm.run_cmd(f"unhook {hook} -> {chain}",
                             nm._privileged(["iptables", "-D", hook,
                                              iface_flag, interface, "-j", chain]),
                             allow_fail=True))


def _drop_chain(chain, steps):
    steps.append(nm.run_cmd(f"flush chain {chain}",
                             nm._privileged(["iptables", "-F", chain]), allow_fail=True))
    steps.append(nm.run_cmd(f"delete chain {chain}",
                             nm._privileged(["iptables", "-X", chain]), allow_fail=True))


def apply_interface(interface):
    """Rebuild and (re)install both chains for one interface from storage.
    Returns the list of StepResults collected."""
    steps = []
    cfg = storage.fw_iface_get(interface)
    rules = storage.fw_rule_list(interface)

    for direction in ("in", "out"):
        chain = _chain(interface, direction)
        hook = _hook(direction)
        iface_flag = _iface_flag(direction)

        _ensure_chain(chain, steps)
        # Always-on protections: keep existing connections (incl. the request
        # that's applying this change) and this dashboard's own port alive.
        steps.append(nm.run_cmd(f"allow established/related ({direction}, {interface})",
                                 nm._privileged(["iptables", "-A", chain, "-m", "conntrack",
                                                  "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT"])))
        if direction == "in":
            steps.append(nm.run_cmd(f"protect dashboard access ({interface})",
                                     nm._privileged(["iptables", "-A", chain, "-p", "tcp",
                                                      "--dport", str(config.PORT), "-j", "ACCEPT"])))

        directional = sorted(
            (r for r in rules if r["direction"] == direction and r.get("enabled", True)),
            key=lambda r: r.get("priority", 100),
        )
        for rule in directional:
            steps.append(nm.run_cmd(f"rule {rule['id']} ({rule.get('description') or 'no description'})",
                                     nm._privileged(["iptables", "-A", chain] + _rule_args(rule))))

        policy = cfg.get(f"{direction}bound_policy", "accept")
        steps.append(nm.run_cmd(f"default policy ({direction}, {interface}) = {policy}",
                                 nm._privileged(["iptables", "-A", chain,
                                                  "-j", _TARGET.get(policy, "ACCEPT")])))
        _ensure_jump(hook, iface_flag, interface, chain, steps)

    return steps


def teardown_interface(interface):
    """Remove the jump rules and delete both chains for one interface."""
    steps = []
    for direction in ("in", "out"):
        chain = _chain(interface, direction)
        _remove_jump(_hook(direction), _iface_flag(direction), interface, chain, steps)
        _drop_chain(chain, steps)
    return steps


def sync_all():
    """Re-apply every enabled interface and tear down any that are stored but
    disabled. Called on app startup so a restart/redeploy is self-healing.
    Entirely a no-op while the global switch is off."""
    if not storage.settings_all().get("firewall_enabled"):
        return
    for name, cfg in storage.fw_iface_all().items():
        if cfg.get("enabled"):
            apply_interface(name)
        else:
            teardown_interface(name)


def disable_all():
    """Tear down every configured interface's chains (used when the global
    switch is turned off) without touching each interface's stored flags."""
    steps = []
    for name in storage.fw_iface_all().keys():
        steps += teardown_interface(name)
    return steps


def panic():
    """Emergency kill switch: tear down every managed chain/jump and disable
    the global switch. Always completes even if individual commands fail."""
    steps = disable_all()
    storage.settings_update({"firewall_enabled": False})
    return steps
