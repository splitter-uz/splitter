"""
Let's Encrypt certificate issuance via certbot — HTTP-01 challenge only.

certbot's `--standalone` authenticator binds the ACME challenge directly on
the mapping's bind IP, port 80, for the few seconds the request takes — no
nginx webroot/vhost wiring needed. Once certbot succeeds, the issued
fullchain/privkey are copied into config.SSL_DIR under the same
`<name>.crt` / `<name>.key` naming every other certificate source
(upload, self-signed) already uses, so nginx_manager's render_conf /
render_http_conf need no changes to serve a Let's Encrypt cert.

Renewal runs on a background thread (see start_scheduler), mirroring
backup.py's scheduler: check periodically, renew anything within
config.LETSENCRYPT_RENEW_WITHIN_DAYS of expiring.
"""
import datetime
import os
import threading
import time

import config
import nginx_manager as nm
import storage
from nginx_manager import StepResult


def _live_dir(domain):
    return f"/etc/letsencrypt/live/{domain}"


def _read_issued(domain):
    """(cert_bytes, key_bytes) from certbot's live directory."""
    live = _live_dir(domain)
    with open(os.path.join(live, "fullchain.pem"), "rb") as fh:
        cert_bytes = fh.read()
    with open(os.path.join(live, "privkey.pem"), "rb") as fh:
        key_bytes = fh.read()
    return cert_bytes, key_bytes


def _install_issued(domain, steps):
    """Copy the certbot-issued (or, in SIMULATE, a demo self-signed) cert into
    SSL_DIR via the same path every other cert source uses. Appends its own
    StepResult(s) to `steps` and returns True/False."""
    if config.SIMULATE:
        # certbot never actually ran (no network, no root, no real DNS) — but
        # openssl runs fine locally, so generate a stand-in cert the same way
        # the self-signed flow already does, purely so the UI has something
        # real to show. Clearly labelled so it's never mistaken for a live cert.
        cert_bytes, key_bytes, gen_step = nm.generate_self_signed(domain)
        gen_step["detail"] = "[simulated — no real ACME order was made] " + gen_step["detail"]
        gen_step["simulated"] = True
        steps.append(gen_step)
        if not gen_step["ok"]:
            return False
    else:
        try:
            cert_bytes, key_bytes = _read_issued(domain)
        except OSError as exc:
            steps.append(StepResult("Read issued certificate", False, str(exc)))
            return False

    _c, _k, save_step = nm.save_certificate(domain, cert_bytes, key_bytes)
    steps.append(save_step)
    return save_step["ok"]


def _certbot_args(domain, email, extra_domains, bind_ip, staging):
    domains = [domain] + [d for d in (extra_domains or []) if d and d != domain]
    args = [
        config.CERTBOT_BIN, "certonly", "-n", "--agree-tos", "--no-eff-email",
        "-m", email,
        "--standalone",
        "--preferred-challenges", "http",
        "--http-01-port", "80",
        "--cert-name", domain,
        "-d", ",".join(domains),
    ]
    if bind_ip:
        args += ["--http-01-address", bind_ip]
    if staging:
        args.append("--staging")
    return args


def request_certificate(domain, email, bind_ip, extra_domains=None, staging=False):
    """Request a new certificate. Returns (ok, steps)."""
    steps = []

    if not config.SIMULATE:
        if not bind_ip:
            steps.append(StepResult(
                "Check HTTP-01 bind address", False,
                "No bind IP given — pick the address that this domain's DNS "
                "points at, so Let's Encrypt can reach the challenge on port 80."))
            return False, steps
        if nm.is_listening(bind_ip, 80):
            steps.append(StepResult(
                "Check port 80", False,
                f"{bind_ip}:80 is already in use — free it (disable whatever's "
                "bound there) before requesting a certificate; the HTTP-01 "
                "challenge needs that port for a few seconds."))
            return False, steps

    args = _certbot_args(domain, email, extra_domains, bind_ip, staging)
    res = nm.run_cmd("Request Let's Encrypt certificate (certbot)", nm._privileged(args))
    steps.append(res)
    if not res["ok"]:
        return False, steps

    return _install_issued(domain, steps), steps


def renew_certificate(domain, bind_ip=None):
    """Force-renew an existing certificate. Returns (ok, steps).

    certbot remembers the authenticator/domains/address from the original
    request (saved under /etc/letsencrypt/renewal/), so `renew` only needs the
    cert name — but the HTTP-01 port-80 requirement is the same as issuance.
    """
    steps = []
    if not config.SIMULATE and bind_ip and nm.is_listening(bind_ip, 80):
        steps.append(StepResult(
            "Check port 80", False,
            f"{bind_ip}:80 is already in use — free it before renewing."))
        return False, steps

    args = [config.CERTBOT_BIN, "renew", "--cert-name", domain,
            "--force-renewal", "--non-interactive", "--no-random-sleep-on-renew"]
    res = nm.run_cmd("Renew Let's Encrypt certificate (certbot)", nm._privileged(args))
    steps.append(res)
    if not res["ok"]:
        return False, steps

    return _install_issued(domain, steps), steps


def delete_certbot_data(domain):
    """Best-effort cleanup of certbot's own bookkeeping for a cert (renewal
    config, account reference, archive). Never blocks the registry delete."""
    args = [config.CERTBOT_BIN, "delete", "--cert-name", domain, "-n"]
    return nm.run_cmd("Remove Let's Encrypt certbot data", nm._privileged(args), allow_fail=True)


# --------------------------------------------------------------------------
# Auto-renewal scheduler (in-app, no system cron needed) — mirrors backup.py
# --------------------------------------------------------------------------
def _parse_openssl_date(s):
    """'Nov 12 08:00:00 2026 GMT' -> aware UTC datetime."""
    s = s.strip()
    if s.endswith(" GMT"):
        s = s[:-4]
    return datetime.datetime.strptime(s, "%b %d %H:%M:%S %Y").replace(
        tzinfo=datetime.timezone.utc)


def _due_for_renewal(rec):
    not_after = nm.cert_info(rec["name"]).get("not_after")
    if not not_after:
        return False   # cert file missing/unreadable — nothing to renew from
    try:
        expiry = _parse_openssl_date(not_after)
    except ValueError:
        return False
    within = datetime.timedelta(days=config.LETSENCRYPT_RENEW_WITHIN_DAYS)
    return expiry - datetime.datetime.now(datetime.timezone.utc) <= within


def check_and_renew_all():
    """Renew every Let's Encrypt cert that's due. Best-effort; never raises."""
    for rec in storage.cert_list():
        if rec.get("source") != "letsencrypt":
            continue
        try:
            if not _due_for_renewal(rec):
                continue
            ok, _steps = renew_certificate(rec["name"], bind_ip=rec.get("bind_ip"))
            if ok:
                storage.cert_add({**rec, **nm.cert_info(rec["name"]), "renewed": _now()})
        except Exception:
            pass   # one bad cert must not stop the others


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_scheduler_started = False


def start_scheduler():
    """Launch the daemon thread that renews due Let's Encrypt certs (idempotent)."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True

    def _loop():
        while True:
            try:
                check_and_renew_all()
            except Exception:
                pass
            time.sleep(6 * 3600)   # renewal only fires within RENEW_WITHIN_DAYS anyway

    threading.Thread(target=_loop, name="letsencrypt-renewal", daemon=True).start()
