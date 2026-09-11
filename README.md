<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-white.png">
    <img src="docs/logo.png" alt="Splitter" width="360">
  </picture>
</p>

<p align="center">
  <strong>A multi‑IP TCP/UDP + HTTP reverse proxy for Linux, driven entirely from your browser.</strong><br>
  Nginx is the data plane. Splitter is the control plane.
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="Python 3" src="https://img.shields.io/badge/Python-3.x-blue.svg?logo=python&logoColor=white">
  <img alt="nginx stream" src="https://img.shields.io/badge/nginx-stream%20%2B%20http-009639.svg?logo=nginx&logoColor=white">
  <img alt="ModSecurity" src="https://img.shields.io/badge/WAF-ModSecurity%20%2B%20CRS-8A2BE2.svg">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED.svg?logo=docker&logoColor=white">
  <img alt="Platform: Linux" src="https://img.shields.io/badge/Platform-Linux-FCC624.svg?logo=linux&logoColor=black">
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-highlights">Highlights</a> ·
  <a href="#-how-it-works">How it works</a> ·
  <a href="#-guides">Guides</a> ·
  <a href="#-running-in-docker">Docker</a> ·
  <a href="#-configuration">Configuration</a> ·
  <a href="#-rest-api">API</a> ·
  <a href="#-whats-new">What's new</a>
</p>

---

## What is Splitter?

Splitter turns one Linux host into a multi‑IP reverse proxy you manage from a
web dashboard instead of config files:

- **Stream (Layer 4)** — raw TCP/UDP passthrough or TLS termination through
  Nginx `stream {}`: databases, SSH, RDP, DNS, WireGuard, anything with a port.
- **Reverse Proxy (Layer 7)** — HTTP/HTTPS with WebSocket, HTTP/2, per‑path
  routing, custom error pages and an optional **ModSecurity + OWASP CRS** WAF in
  front.

Type a domain, pick an interface (or a dedicated sub‑interface with its own IP),
point an upstream pool at your backends — static addresses or **Docker
containers / Swarm services picked from a live list** — and it validates and
reloads in seconds. Reusable settings (rate limits, timeouts, log formats,
error pages, config blocks, log rotation) live on one **Snippets** page and are
picked per mapping. **No hand‑edited config, no manual `ip` commands.**

Because it runs **on the host** (not inside a network namespace), the
interfaces and IPs it creates are real and reachable on the LAN, and Nginx
binds them directly.

<p align="center">
  <img src="docs/dashboard.png" alt="Splitter dashboard" width="900">
</p>

Built with **Python + Flask** and a **Tailwind** single‑page UI. **No database**:
mappings, users and snippets persist as human‑readable JSON, and every apply
streams back the exact steps that ran.

---

## 🚀 Quick start

### Docker (recommended)

One self‑contained image — Nginx with the stream module, ModSecurity + CRS,
certbot, logrotate, the diagnostic CLIs and the app. It runs in the **host
network namespace** so the IPs it creates are real, and auto‑detects your uplink
NIC:

```bash
git clone <repo> splitter && cd splitter
docker compose up -d --build
```

Open **`http://<server-ip>:8088`** and create the admin account on first load.
See [Running in Docker](#-running-in-docker) for volumes and hardening.

### Native (systemd)

```bash
git clone <repo> splitter && cd splitter
sudo ./setup.sh --service        # installs deps, configures nginx + kernel, starts the service
```

`setup.sh` is idempotent. It installs the dependencies (nginx + stream module,
python3/venv, iproute2, a DHCP client, openssl, iptables, certbot, logrotate and
the Tools‑page CLIs), wires the top‑level `stream {}` include, enables
`net.ipv4.ip_nonlocal_bind`, creates `/var/lib/splitter`, builds the venv and,
with `--service`, installs a **systemd** unit.

<details>
<summary><code>setup.sh</code> options &amp; service management</summary>

| Flag | Meaning |
|---|---|
| `--service` | Install + enable the systemd service. |
| `--nic NAME` | Force the parent interface (else auto‑detected). |
| `--host ADDR` | UI bind address (default `0.0.0.0`). |
| `--port N` | UI port (default `8088`). |
| `--no-deps` | Skip package installation. |
| `--waf` / `--waf-only` / `--waf-enforce` | Install / repair / enforce the WAF. |

```bash
systemctl status splitter
journalctl -u splitter -f        # apply failures are logged here with the failing step
systemctl restart splitter       # after every git pull, to load new code
```

Run without a service: `sudo ./setup.sh` (configure only) then `sudo ./run.sh`.
</details>

> [!IMPORTANT]
> The UI binds `0.0.0.0:8088` and is protected by login, but it performs
> privileged host actions. Keep it on a trusted network (or behind an
> authenticated reverse proxy / SSH tunnel with `--host 127.0.0.1`) and use a
> strong admin password.

---

## ✨ Highlights

<table>
<tr>
<td width="50%" valign="top">

**🔀 Stream (L4) + Reverse Proxy (L7)**
Two separate panels; the form only shows what applies. Stream: TCP/UDP
passthrough or TLS termination. Reverse Proxy: WebSocket, HTTP/2, Force‑HTTPS
+ HSTS, custom `location` blocks, and a one‑click WAF.

**🗺️ Path & method routing**
Each custom location can use its **own backend pool** and be limited to certain
**HTTP methods** — `/api` GET stays on the main pool, POST/PUT go to a write
node, DELETE elsewhere. A `/` row splits the whole site by method.

**⚖️ Load balancing & failover**
`upstream {}` pools with round‑robin, `least_conn`, consistent `hash`, `random`
or `random two`; **active‑passive priority tiers** with TCP/HTTP health probing
and flap‑protected fail‑back.

**🧩 Snippets**
Rate limits, timeouts, log formats (incl. JSON), error pages, raw config
blocks and log‑rotation policies as named, reusable items — one picker on the
mapping form, another per custom location. Editing a snippet re‑applies every
mapping that uses it.

**📜 Logs with history**
Per‑mapping access/error logs rotated daily by `logrotate` (dated, gzip,
per‑mapping retention). Live tail plus a **time‑range search** across the live
file *and* rotated archives — "what hit this mapping two days ago at 15:40?".

**🧭 Interfaces & sub‑interfaces**
Auto‑detected physical / VLAN interfaces; optional managed **macvlan / ipvlan
sub‑interfaces** with their own MAC + IP (static or DHCP), inside an 802.1Q
VLAN if you like.

</td>
<td width="50%" valign="top">

**🐳 Docker & Swarm backends**
Pick running containers or Swarm services **by name** straight into a pool. A
reconciler and a real‑time Docker **events** watcher keep addresses current —
a recreated container rejoins the pool in a fraction of a second.

**🔒 SSL**
Upload, generate a SAN self‑signed cert, request one from **Let's Encrypt**
(HTTP‑01, auto‑renew) or reuse a managed cert across mappings.

**🛡️ WAF (ModSecurity + OWASP CRS)**
Baked into the image; **Install / Off / Detection / Enforce** from the WAF
page. Creating a Reverse Proxy mapping binds it behind the WAF automatically.

**🛑 Access lists & 🧱 firewall**
Named CIDR allow/deny lists (a built‑in **tas‑ix** list auto‑refreshes) and a
security‑group‑style per‑interface `iptables` firewall with lockout protection
and a Panic switch. Both off until you opt in.

**📊 Monitoring & tools**
Host CPU/RAM/disk/network, live **nginx `stub_status`** counters (active /
reading / writing / waiting, req/s) from a loopback‑only endpoint Splitter
provisions, a live routing map, and a Tools page: ping, port test, **nmap port
scanner**, DNS, traceroute, tcpdump, WHOIS, SSL check.

**🔐 Auth, audit, backups**
Admin / creator / viewer roles, an Activity audit log, mapping export/import
and full‑system snapshots with scheduled backups and one‑click rollback.
Every option on the mapping form carries an **ⓘ tooltip**.

</td>
</tr>
</table>

---

## 🏗️ How it works

Each domain can get its **own IP** on the host. A **Stream** mapping makes Nginx
`stream` listen on that IP and balance across the pool at Layer 4; a **Reverse
Proxy** mapping terminates HTTP(S), optionally runs each request through
ModSecurity/CRS, and proxies at Layer 7.

<p align="center">
  <img src="docs/architecture.png" alt="Splitter architecture" width="960">
</p>

On every **Save / Apply**:

1. **Resolve the bind IP** — the chosen interface's address, or the selected sub‑interface's.
2. **Generate** `/etc/nginx/stream.d/<domain>.<port>.conf` (Stream) or an L7 server block in `conf.d` (Reverse Proxy), with an `upstream {}` pool, the selected snippets and routing rules.
3. **Validate** with `nginx -t` and **reload** (or stop + start when a reload can't bind a fresh IP). A failing config is rolled back — the live proxy is never reloaded against a broken block.

The provisioning pipeline mirrors the commands you'd otherwise run by hand:

```bash
ip link add link eth0 name eth0.50 type vlan id 50          # if a VLAN ID is given
ip link add link eth0.50 name mv-site4-0 type macvlan mode bridge   # (ipvlan on VMware)
ip addr add 192.168.50.15/24 dev mv-site4-0                 # or: dhclient mv-site4-0
sysctl -w net.ipv4.ip_nonlocal_bind=1                       # bind not-yet-up IPs
nginx -t && nginx -s reload                                 # stop+start if reload won't bind
```

A generated Stream block:

```nginx
upstream upstream_924cb235c2 {
    least_conn;
    server 192.168.50.10:443;
    server 192.168.50.11:443;
}
server {
    listen 192.168.50.15:443;
    include /etc/nginx/acl.d/_default.conf;   # only when an access list is selected
    proxy_pass upstream_924cb235c2;
    proxy_timeout 10m;
    proxy_connect_timeout 5s;
}
```

A Docker‑backed backend renders the same way; the stored backend is the
**container name**, and its `server` line is resolved to the container's current
IP at generation time and kept current automatically.

**Docker & Swarm discovery.** Splitter runs in the host network namespace, so
it can't rely on Docker's embedded DNS. It talks to the Engine API over the unix
socket to list running containers (name, network IPs, exposed ports) — or, on a
Swarm manager, services — and turns a chosen one into a concrete address the
host can reach (the host has routes to the docker bridges). A periodic
reconciler re‑resolves every Docker‑backed backend, and a Docker **events**
watcher fires an immediate reconcile on start / stop / die / health changes, so
the pool follows the containers in real time.

---

## 🧭 Guides

**Creating a mapping**

1. **Log in** (create the admin on first run).
2. Pick **Stream** or **Reverse Proxy** (or start from the **Docker** page to pick containers).
3. **Domain** — e.g. `site4.example.com`, and the listen port.
4. **Bind target** — a physical interface (binds its existing IP) or a managed sub‑interface (dedicated IP).
5. **Backend pool** — one or more `host:port`, or containers from the grid. Two or more backends reveal the load‑balancing and failover options.
6. **Snippets** — rate limit, timeouts, log format, log rotation, error pages, config blocks. Leave empty for the defaults.
7. **SSL** — none / passthrough, or a managed certificate (upload, self‑signed, Let's Encrypt).
8. **Access list**, then **Preview** the generated config or **Save / Apply**.

<details>
<summary>🧩 <strong>Snippets</strong> — rate limits, timeouts, log formats, error pages, config blocks, log rotation</summary>

<br>

The **Snippets** page (admin) holds every reusable setting as a named item, one
tab per kind:

| Kind | What it holds | Rendered as |
|---|---|---|
| **Rate limits** | max connections per client IP, download / upload rate | Stream: `limit_conn`, `proxy_download_rate`, `proxy_upload_rate` · Reverse Proxy: `limit_conn`, `limit_rate` |
| **Timeouts** | `proxy_timeout`, `proxy_connect_timeout` | Stream as‑is · Reverse Proxy: `proxy_read/send_timeout`, `proxy_connect_timeout` |
| **Log formats** | a `log_format` body (presets incl. JSON), stream or http context | `log_format <mapping>_fmt …` |
| **Error pages** | uploaded HTML/Jinja2 for a status code or range | `proxy_intercept_errors on` + `error_page` + an internal location serving a static export |
| **Config snippets** | raw nginx directives, server / location scope | an `include` file under `conf.d/splitter-snippets/` |
| **Log rotation** | keep N days, gzip, optional max size | a logrotate stanza (see Logs) |

On a mapping (Stream, Reverse Proxy or Docker — same form) pick what you need
under **Snippets**: one rate limit, one timeouts set, one log format, one
rotation policy, any error pages and config snippets. Each **custom location**
row has its own picker (config / rate limit / timeouts for that path only).
Nothing selected keeps the built‑in defaults. Error pages and config snippets
only take effect on Reverse Proxy / WAF mappings; the rest apply to both kinds.
Editing a snippet re‑applies every mapping that uses it (a broken change is
rolled back by `nginx -t`), and a snippet still in use can't be deleted.
Mappings saved before this existed keep their inline values until re‑saved.
</details>

<details>
<summary>🗺️ <strong>Path & method routing</strong> — send /api POSTs somewhere else</summary>

<br>

In a Reverse Proxy mapping's **Custom locations**, each row has a path, optional
**backends for this path** (`host:port` list → its own `upstream`), optional
**methods** (only those go to that pool; the rest stay on the main pool) and
optional extra directives. Several rows may share a path for a multi‑way split:

| Path | Backends | Methods | Result |
|---|---|---|---|
| `/api` | — | — | main pool |
| `/api` | `10.0.0.21:8080` | `POST, PUT` | write node |
| `/api` | `10.0.0.22:8080` | `DELETE` | another node |

A row with path `/` splits the whole site by method. **Preview** on the Reverse
Proxy form shows the generated server block (routing, snippets, error pages).
</details>

<details>
<summary>📜 <strong>Logs</strong> — live tail, time‑range search, rotation & retention</summary>

<br>

Every mapping writes `/var/log/splitter/<domain>.<port>/…-access.log` and
`…-error.log`. The **Logs** page tails them live with search, and a **Time
range** row searches a window ("last 24h", or *2 days ago 15:30 → 16:00*)
across the live file **and every rotated archive** (gzip included), matching
each line's own timestamp. Results are prefixed with the archive date; **Files &
rotation** lists the archives with downloads.

Rotation runs through the real `logrotate` (installed with Splitter): daily,
`-YYYYMMDD` suffix, gzip, deleted after the retention period, `nginx -s reopen`
afterwards. Splitter regenerates the config and runs it hourly from a background
thread, so no cron is needed in the container; **Rotate now** forces a run. The
defaults (7 days, gzip, optional max size) are on the Logs page; a **Log
rotation** snippet on a mapping overrides them for that mapping.
</details>

<details>
<summary>🛑 <strong>Access lists</strong> — allow/deny, Stream or Reverse Proxy</summary>

<br>

The **Access Lists** page renders each list to `/etc/nginx/acl.d/<name>.conf`
(`allow` lines + a final `deny all;`); a mapping's `server {}` pulls in the one it
selected via `include`. Create your own (paste CIDRs), set a **Source URL** to
auto‑refresh on an interval, or pick a **global default** that every "use default"
mapping follows with no re‑apply. The built‑in **`tasx`** (tas‑ix / Uzbekistan)
list ships with auto‑refresh enabled.

```nginx
# /etc/nginx/acl.d/tasx.conf — managed by Splitter
allow 82.148.0.0/21;
allow 217.30.160.0/20;
# … hundreds more
allow 127.0.0.1/32;
deny all;
```
</details>

<details>
<summary>🧱 <strong>Firewall</strong> — per‑interface iptables (security‑group model)</summary>

<br>

<p align="center">
  <img src="docs/firewall.png" alt="Splitter per-interface firewall model" width="820">
</p>

Every interface gets its own ordered rule set (`SFW-IN-<iface>` / `SFW-OUT-<iface>`
chains): protocol, port/range, source CIDR, accept/drop/reject, priority, plus a
default policy. **Two safety switches, both off by default** (a global master and a
per‑interface enforce toggle) so installing changes nothing until you opt in. Every
managed chain always allows established connections and this dashboard's port
**first**, and a one‑click **Panic** button tears every managed chain down instantly.
</details>

<details>
<summary>🛡️ <strong>WAF</strong> — ModSecurity + OWASP CRS</summary>

<br>

Front the UI with a self‑hosted WAF: Nginx terminates TLS on **8443**, filters every
request through ModSecurity/CRS (SQLi, XSS, RCE, …) with per‑IP login rate limits,
and proxies to the app. Enable it from the **WAF page** (admin): **Install / Repair**,
then switch **Off / Detection / Enforce**. It starts in **DetectionOnly** so it can't
lock you out.

Creating a mapping from the **Reverse Proxy** tab automatically binds it behind the
WAF — no separate step. The WAF page also lists every bound mapping under
**Protected apps**, where you can **unbind** one back to plain HTTP if needed.
(Only HTTPS/TLS‑terminating mappings are eligible; UDP and TLS‑passthrough stay
Stream‑only.)
</details>

---

## 🐳 Running in Docker

The container runs in the **host network namespace** with the privileges Splitter
needs to create real macvlan/VLAN interfaces, bind their IPs in Nginx and manage
`iptables`. Docker here is a **packaging convenience**, not an isolation boundary.

```bash
docker compose up -d --build      # build + start
docker compose logs -f splitter   # watch startup / apply steps
docker compose down               # stop (named volumes keep your data)
```

**The image bundles** Nginx with the stream module, iproute2, iptables, a DHCP
client, openssl, `certbot`, `logrotate`, the diagnostic CLIs (incl. `nmap` and
`tcpdump`), and **ModSecurity + OWASP CRS** so the WAF works and survives
redeploys (it starts off). The entrypoint auto‑detects the uplink NIC from the
default route, sets `ip_nonlocal_bind`, starts Nginx and launches the app.
Splitter then provisions its own loopback `stub_status` endpoint, exports error
pages and snippet includes, and starts the background schedulers (health,
failover, Docker reconcile, Let's Encrypt renewal, backups, log rotation).

**Compose highlights**

```yaml
services:
  splitter:
    build: .
    network_mode: host          # real host IPs, reachable on the LAN
    privileged: true            # ip / iptables / sysctl / tcpdump
    environment:
      SPLITTER_NIC: "auto"          # detected from the default route at startup
      SPLITTER_REBOOT_CMD: "true"   # reboot-host can't work from inside a container
    volumes:
      - splitter-data:/var/lib/splitter        # mappings, users, keys (source of truth)
      - splitter-streamd:/etc/nginx/stream.d    # generated stream configs
      - splitter-ssl:/etc/nginx/ssl             # managed certificates
      - splitter-acl:/etc/nginx/acl.d           # access-list snippets
      - splitter-confd:/etc/nginx/conf.d        # WAF / L7 app blocks
      - splitter-logs:/var/log/splitter         # per-mapping nginx logs
      - /var/run/docker.sock:/var/run/docker.sock:ro   # powers the Docker page
      - /etc/resolv.conf:/etc/resolv.conf       # edit the REAL host DNS
      - /etc/hosts:/etc/hosts                   # edit the REAL host hosts file
    restart: unless-stopped
```

> [!NOTE]
> **Runs on a Linux host.** Docker Desktop (macOS/Windows) has no real LAN NIC
> in its VM. The **reboot‑host** button can't work from inside a container, and
> some networks (phone hotspots, certain switches) block the extra MACs that
> macvlan needs — that's physics, not Docker.

---

## ⚙️ Configuration

<details>
<summary>Environment variables</summary>

<br>

| Variable | Default | Purpose |
|---|---|---|
| `SPLITTER_NIC` | auto / `eth0` | Default parent interface (`auto` in Docker). |
| `SPLITTER_BIND_PREFIX` | `24` | CIDR prefix for static IPs. |
| `SPLITTER_DATA_DIR` | `/var/lib/splitter` | Where `mappings.json` / `users.json` live. |
| `SPLITTER_STREAM_DIR` | `/etc/nginx/stream.d` | Where `.conf` files are written. |
| `SPLITTER_SSL_DIR` | `/etc/nginx/ssl` | Where cert/key are saved. |
| `SPLITTER_RESOLV_CONF` | `/etc/resolv.conf` | DNS file the Interfaces page edits. |
| `SPLITTER_HOSTS_FILE` | `/etc/hosts` | Hosts file the Interfaces page edits. |
| `SPLITTER_DHCP_ACQUIRE_CMD` | `timeout 25 dhclient -1 {iface}` | DHCP client command. |
| `SPLITTER_SUDO` | `""` if root else `sudo` | Prefix for privileged commands. |
| `SPLITTER_RELOAD_CMD` / `SPLITTER_RESTART_CMD` | nginx binary | How Nginx is reloaded/restarted. |
| `SPLITTER_IP_NONLOCAL_BIND` | `1` | Bind an address that isn't fully up yet. |
| `SPLITTER_LETSENCRYPT_RENEW_DAYS` | `30` | Auto‑renew a Let's Encrypt cert within this many days of expiring. |
| `SPLITTER_HOST` / `SPLITTER_PORT` | `0.0.0.0` / `8088` | UI bind. |
| `SPLITTER_STATUS_PORT` | `8090` | Loopback port of the nginx `stub_status` endpoint Splitter provisions for Monitoring. |
| `SPLITTER_SNIPPET_DIR` | `/etc/nginx/conf.d/splitter-snippets` | Where config‑snippet `include` files are written. |
| `SPLITTER_SIMULATE` | auto | `1` = dry‑run; auto‑on when not on a Linux nginx host. |

</details>

<details>
<summary>Running as a non‑root user</summary>

<br>

The default runs as **root** (no sudo needed). To run unprivileged, install the
least‑privilege sudoers snippet (it grants only the exact `ip`, `nginx`, `sysctl`,
`dhclient`, and scoped `iptables` commands the tool needs):

```bash
sudo cp deploy/splitter.sudoers /etc/sudoers.d/splitter
sudo chmod 0440 /etc/sudoers.d/splitter
sudo visudo -cf /etc/sudoers.d/splitter        # validate
```

then set `User=splitter` in the unit and `SPLITTER_SUDO=sudo`.
</details>

---

## 🔌 REST API

Every UI action is a JSON endpoint under `/api/`, session‑authenticated with
the same admin / creator / viewer roles as the dashboard (`viewer` is read‑only
and gets `403` on any write).

<details>
<summary>Full endpoint reference</summary>

<br>

| Method | Path | Role | Description |
|---|---|---|---|
| `POST` | `/api/setup` | — | Create the initial admin (first run only). |
| `POST` | `/api/login` · `/api/logout` | — | Start / end a session. |
| `GET` | `/api/auth/status` | — | Current session (if any). |
| `GET` | `/api/config` | any | Effective config + mode. |
| `GET` | `/api/interfaces` · `/interfaces/traffic` | any | Detected interfaces + live per‑iface traffic. |
| `GET`·`POST`·`DELETE` | `/api/subinterfaces[/<name>]` | admin | List / create / edit / delete sub‑interfaces. |
| `GET`·`POST` | `/api/settings` | any / admin | Read / change tool‑wide settings. |
| `GET`·`POST` | `/api/network/dns` · `/network/hosts` | admin | View / edit host DNS + `/etc/hosts`. |
| `POST` | `/api/tools/{ping,port,portscan,dns,traceroute,whois,tcpdump,routes,sslcheck}` | admin/creator | Network diagnostics. |
| `GET`·`POST`·`DELETE` | `/api/mappings[/<domain>]` | varies | List / create+provision / deprovision; `/toggle`, `/diagnose`. |
| `POST` | `/api/preview` | any | Render the conf without applying. |
| `GET` | `/api/health` | any | Cached per‑backend up/down rollup for every mapping. |
| `GET`·`POST`·`DELETE` | `/api/access-lists[/<name>]` | any / admin | Manage access lists (+ `/refresh`). |
| `GET`·`POST`·`DELETE` | `/api/ssl/certs[/<name>]` | any / admin/creator | List; upload / self‑sign / **Let's Encrypt** / delete / `/renew`. |
| `GET` | `/api/backup` | admin/creator | Export the mappings store as JSON (not for viewers). |
| `GET` | `/api/certs` | any | Certs a mapping can reuse (dropdown source). |
| `GET` | `/api/docker/status` · `/docker/containers` · `/docker/services` | any | Docker/Swarm discovery for the backend picker. |
| `GET`·`POST`·`DELETE` | `/api/forward-proxies[/<name>]` | any / admin/creator | Standalone outbound forward proxies (+ `/toggle`, admin). |
| `GET`·`POST`·`DELETE` | `/api/firewall/*` | admin | Overview, settings, per‑iface, rules, panic, `/whoami`. |
| `GET`·`POST`·`DELETE` | `/api/backups[/*]` | admin | Snapshots, download, restore, schedule. |
| `POST` | `/api/reapply` | admin | Re‑provision every stored mapping. |
| `GET`·`POST`·`DELETE` | `/api/users[/<username>]` | admin | List / create / remove users. |
| `POST` | `/api/account/password` | any | Change your own password. |
| `GET`·`POST` | `/api/waf/{status,install,mode,settings,bind,unbind,apps}` | admin | Manage the WAF. |
| `GET` | `/api/activity` | admin | Audit log (logins, mapping/user changes). |
| `GET` | `/api/logs` · `/logs/<domain>/<port>/<kind>` | admin | Per‑mapping access/error logs. |
| `GET` | `/api/metrics` · `/api/traffic` | any | Host CPU/RAM/disk/network + per‑mapping traffic. |
| `GET`·`POST` | `/api/nginx/status` · `/nginx/status/provision` | any / admin | nginx `stub_status` counters (http layer) + (re)provision the loopback status endpoint. |
| `GET`·`POST`·`DELETE` | `/api/error-pages[/<key>]` (+ `/preview`) | any / admin | Uploaded error pages: dashboard errors and, when picked on a mapping, nginx `error_page`. |
| `GET`·`POST`·`DELETE` | `/api/log-formats[/<name>]` | any / admin | Log‑format snippets (Snippets page); picked on a mapping as `logformat:<name>`. |
| `GET`·`POST`·`DELETE` | `/api/config-snippets[/<name>]` | any / admin | Config snippets: raw nginx blocks written as include files a mapping pulls into its Advanced config / custom locations. |
| `GET` · `GET`·`POST`·`DELETE` | `/api/snippets` · `/api/snippets/{ratelimit,timeouts,logrotate}[/<name>]` | any / admin | Snippet catalogue for the mapping form's picker (all kinds), plus rate‑limit, timeout and log‑rotation snippets. A mapping stores `snippets` refs like `ratelimit:api`. |
| `GET` · `POST` | `/api/logs/<domain>/<port>/<kind>/{search,files}` · `/api/logs/rotate` · `/api/logs/rotation` | admin | Time‑range search across live + rotated (.gz) logs, archive listing/download (`/download?file=`), run logrotate now, rotation status. |
| `GET` | `/api/random-mac` | any | Generate a random locally‑administered MAC. |
| `POST` | `/api/system/reboot` | admin | Reboot the host (native install only). |

</details>

---

## 🗂️ Project layout

<details>
<summary>Repository structure &amp; on‑host state</summary>

<br>

```
splitter/
├── backend/                  # Python backend (Flask + REST API)
│   ├── app.py                #   routes + auth
│   ├── nginx_manager.py      #   the only module that touches the OS
│   ├── failover.py           #   active-passive priority failover
│   ├── health.py             #   cached per-backend up/down probing
│   ├── firewall.py           #   per-interface iptables chains
│   ├── access.py             #   access lists + tas-ix refresh
│   ├── waf.py                #   ModSecurity/CRS management
│   ├── letsencrypt.py        #   certbot issuance + background renewal
│   ├── docker_detect.py      #   Docker/Swarm container & service discovery
│   ├── docker_reconcile.py   #   periodic re-resolve of Docker-backed backends
│   ├── docker_events.py      #   real-time Docker events -> instant reconcile
│   ├── activity.py           #   audit log
│   ├── metrics.py            #   host CPU/RAM/disk/network for Monitoring
│   ├── nginx_status.py       #   provisions + polls nginx stub_status (Monitoring / live map)
│   ├── snippets.py           #   snippet kinds, "kind:name" refs -> rendered fields
│   ├── config_snippets.py    #   raw nginx include files (Snippets page)
│   ├── error_pages.py        #   custom error pages (dashboard + nginx static exports)
│   ├── logrotate.py          #   per-mapping log rotation + time-range log search
│   ├── storage.py            #   atomic JSON persistence
│   └── … (auth, config, validators, backup, net_*)
├── frontend/                 # Tailwind single-page UI
├── deploy/                   # systemd unit · least-privilege sudoers · nginx snippet
├── Dockerfile · docker-compose.yml · docker/entrypoint.sh
├── setup.sh                  # one-shot native installer
└── run.sh
```

On‑host state (survives redeploys):

```
/var/lib/splitter/*.json           # mappings, users, sub-interfaces, settings, firewall…
/var/lib/splitter/backups/*.zip     # full-system snapshots
/etc/nginx/stream.d/<domain>.conf   # generated Stream configs
/etc/nginx/conf.d/*.conf            # generated Reverse Proxy / WAF configs
/etc/nginx/conf.d/splitter-snippets/*.inc   # config-snippet include files
/etc/nginx/conf.d/splitter-status.conf      # loopback stub_status server (Monitoring)
/var/lib/splitter/error_pages_nginx/*.html  # static exports of error pages for nginx
/var/lib/splitter/logrotate.{conf,state}    # generated rotation config + logrotate state
/var/log/splitter/<domain>.<port>/          # per-mapping access/error logs + rotated .gz archives
/etc/nginx/ssl/<domain>.{crt,key}   # managed certificates (upload, self-signed, Let's Encrypt)
/etc/letsencrypt/live/<domain>/     # certbot's own copy (source for the above)
```
</details>

---

## 🔒 Security notes

- **Login required** — passwords are stored only as salted PBKDF2 hashes; the last admin can't be deleted; sessions are cookie‑only.
- **No shell injection** — every command runs as an argv list, never a shell string; tool inputs (hosts, ports, filters, nmap targets) are validated.
- **Validated input** — domains (RFC‑1123), IPs, `host:port` backends, MACs, VLAN IDs, HTTP methods, snippet names; log downloads are resolved from the stored mapping, never from a user‑supplied path.
- **Fail‑safe reloads** — `nginx -t` before every reload; a bad block, snippet or format is rolled back so the live proxy keeps its last good config.
- **Loopback‑only internals** — the `stub_status` endpoint listens on `127.0.0.1` and error‑page exports are served through an `internal` location.
- **Atomic writes** for every store; **0600** for private keys.

---

## 🩹 Troubleshooting

<details>
<summary>Common issues</summary>

<br>

- **`bind() … (99: Cannot assign requested address)`** — the bind IP wasn't fully up. Ensure `sysctl net.ipv4.ip_nonlocal_bind` prints `1`.
- **A change "didn't take"** — after `git pull`, run `sudo systemctl restart splitter` (native) or `docker compose up -d --build` (Docker) to load new code.
- **An apply returns 500** — `journalctl -u splitter -f` (or `docker compose logs -f`) logs the failing step; the UI's *Provisioning Steps* panel shows the same.
- **Docker page is empty / hidden** — Splitter couldn't reach the Docker Engine socket. `docker-compose.yml` mounts `/var/run/docker.sock` read‑only by default; if you removed that line, or run natively, make sure the process has read access to `/var/run/docker.sock`.
</details>

---

## 🆕 What's new

This release adds, on top of the L4/L7 proxy, WAF, Docker backends, failover, SSL and firewall of 0.1.0:

- **Snippets page** — rate limits, timeouts, log formats, error pages, config blocks and log‑rotation policies as reusable items; one picker on the mapping form and one per custom location. Per‑mapping rate‑limit / timeout / log‑format / error‑page fields are gone from the form (mappings saved earlier keep their inline values until re‑saved).
- **Path & method routing** — per‑location backend pools and HTTP‑method splits, rendered as dedicated upstreams and a `map $request_method`. Rate limits and timeouts now also apply to Reverse Proxy mappings.
- **Custom error pages served by nginx** — uploaded pages can be selected on a mapping (`proxy_intercept_errors` + `error_page`), not just used by the dashboard.
- **Logs** — daily rotation with retention and gzip via `logrotate`, a time‑range search across live and archived logs, and per‑archive downloads.
- **Monitoring** — nginx `stub_status` counters on the Monitoring page and the live map, from a self‑provisioned loopback endpoint.
- **Tools** — an nmap‑based port scanner (with a pure‑Python fallback).
- **UX** — ⓘ tooltips on every mapping‑form option; Error Pages moved into the Snippets page.

**Upgrading:** `git pull` then `docker compose up -d --build` (or `sudo ./setup.sh --service` natively — it installs the new `logrotate` and `nmap` dependencies). Existing mappings keep working unchanged; the new `stub_status` server block and rotation config are provisioned automatically on start.

---

## 📄 License

Released under the [MIT License](LICENSE) — © 2026 Splitter Contributors.
