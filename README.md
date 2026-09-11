<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-white.png">
    <img src="docs/logo.png" alt="Splitter" width="360">
  </picture>
</p>

<p align="center">
  Turn a Linux box into a multi‑IP TCP/UDP + HTTP reverse proxy you drive from your browser.
</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/License-MIT-green.svg">
  <img alt="Python 3" src="https://img.shields.io/badge/Python-3.x-blue.svg?logo=python&logoColor=white">
  <img alt="nginx stream" src="https://img.shields.io/badge/nginx-stream-009639.svg?logo=nginx&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-ready-2496ED.svg?logo=docker&logoColor=white">
  <img alt="Platform: Linux" src="https://img.shields.io/badge/Platform-Linux-FCC624.svg?logo=linux&logoColor=black">
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-features">Features</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-docker-and-swarm-backends">Docker & Swarm</a> ·
  <a href="#-running-in-docker">Running in Docker</a> ·
  <a href="#-configuration">Configuration</a> ·
  <a href="#-rest-api">API</a>
</p>

---

## What is Splitter?

**Splitter** turns a Linux host into a multi‑IP reverse proxy you manage entirely
from a web dashboard — **both** a Layer‑4 **Stream** proxy (raw TCP/UDP
passthrough or termination) **and** a Layer‑7 **Reverse Proxy** (HTTP, with
ModSecurity/WAF inspection, WebSocket, HTTP/2, per‑path locations). Type a
domain, pick an interface (or a dedicated sub‑interface you provisioned), point
an upstream pool at your backends — static addresses, or **Docker
containers/Swarm services picked straight from a live list** — and it reloads in
a few seconds. **No manual `ip` commands, no hand‑edited config.**

Because it runs **directly on the host** (no container namespaces hiding the
network), the interfaces and IPs it creates are **real and reachable on the LAN**,
and Nginx binds them directly.

<p align="center">
  <img src="docs/dashboard.png" alt="Splitter dashboard" width="900">
</p>

Stream and Reverse Proxy mappings are two clearly separate panels in the UI —
Reverse‑Proxy‑only fields (WebSocket, HTTP/2, custom `location` blocks,
Force‑HTTPS/HSTS) simply don't appear while you're building a Stream mapping,
because nginx never reads them for one.

On every **Save / Apply**, the host:

1. **Resolves the bind IP** — the chosen interface's address, or a selected sub‑interface's.
2. **Generates** `/etc/nginx/stream.d/<domain>.conf` (Stream) or an L7 server block (Reverse Proxy) with an `upstream {}` pool.
3. **Validates** (`nginx -t`) and **reloads** (falling back to a full restart if a reload won't bind a fresh IP).

Built with **Python + Flask** and a **Tailwind** single‑page UI. **No database** —
mappings and users persist to human‑readable JSON. The dashboard streams back a
step‑by‑step log of exactly which commands ran.

---

## 🚀 Quick start

### Option A — Docker (fastest)

Splitter ships as a single self‑contained image (Nginx + stream module + WAF +
the app). It runs in the **host network namespace** so the IPs it creates are
real, and **auto‑detects your uplink NIC** — you just bring it up:

```bash
git clone <repo> splitter && cd splitter
docker compose up -d --build
```

Then open **`http://<server-ip>:8088`** and create the admin account on first load.
See [Running in Docker](#-running-in-docker) for details, volumes, and hardening.

### Option B — Native (systemd)

```bash
git clone <repo> splitter && cd splitter
sudo ./setup.sh --service        # installs deps, configures nginx + kernel, starts the service
```

`setup.sh` is idempotent and safe to re‑run. It installs dependencies (nginx +
stream module, python3/venv, iproute2, dhcp client, openssl, iptables), wires the
top‑level `stream {}` include, enables `net.ipv4.ip_nonlocal_bind`, creates the
persistent store at `/var/lib/splitter`, builds the venv, and (with `--service`)
installs a **systemd** unit running as root.

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
> The UI binds `0.0.0.0:8088` and is **protected by login**, but it performs
> privileged host actions — restrict it to a trusted network (or put it behind
> an authenticated reverse proxy / SSH tunnel with `--host 127.0.0.1`), and use a
> strong admin password.

---

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

**🔐 Auth & roles**
First run creates an **admin**; after that the whole UI requires login. Admins
add **admin**/**creator**/**viewer** users, delete, import, re‑apply, and manage
everything. Creators add and edit; **viewers** are read‑only (they see every
page's data but every create/edit/delete control is hidden and the API refuses
them with 403).
An **Activity** page keeps an audit log of logins, mapping changes, and
user‑management actions.

**🧭 Dynamic interface detection**
Discovers physical & VLAN interfaces (`ip -j addr`) and offers them in a dropdown.

**🧩 Managed sub‑interfaces**
Global toggle (off by default = bind the interface's existing IP). On = create
**static macvlan/ipvlan sub‑interfaces** with their own MAC + IP, optionally inside
an **802.1Q VLAN**, as managed resources a mapping simply selects.

**🔀 Stream (L4) + Reverse Proxy (L7), split cleanly**
Two dedicated panels/tabs. **Stream**: raw TCP/UDP passthrough or TLS
termination via Nginx `stream {}`. **Reverse Proxy**: HTTP with WebSocket,
HTTP/2, per‑path `location` blocks, and — front it with ModSecurity/CRS in one
click, no separate config to write.

**🗺️ Path & method routing**
On a Reverse Proxy mapping each custom location can point at its **own backend
pool** and restrict that to certain **HTTP methods** — `/api` GET stays on the
main pool while POST/PUT go to a write node, DELETE to a third. Several rows
may share a path for a multi‑way split; a `/` row splits the whole site by
method. Rendered as one `upstream` per pool and a `map $request_method`.

**⚖️ Load‑balancing pool**
One or many backends rendered as `upstream {}`, with a selectable method:
round‑robin, `least_conn`, `hash $remote_addr` (+`consistent`), `random`, or
`random two least_conn` — auto‑revealed once a mapping has 2+ backends.

**🔁 Active‑passive failover**
N‑level **priority tiers** with active TCP/HTTP health probing: traffic stays on
the lowest‑priority tier that has a healthy backend, and fails back (with flap
protection) when a better tier recovers.

</td>
<td width="50%" valign="top">

**🐳 Docker & Swarm‑backed backends**
A dedicated **Docker** page lists running containers (or, on a Swarm manager,
services) and lets you pick them straight into a mapping's backend pool — by
**name**, not a hand‑typed IP. A background reconciler re‑resolves each
Docker‑backed backend and a live **Docker events** watcher reacts to
container start/stop/health changes in real time, re‑rendering and reloading
automatically — Traefik‑style, but nginx stays the data plane.

**🔒 Automated SSL**
Upload your own cert/key, generate a SAN self‑signed cert with `openssl`,
**request one from Let's Encrypt** (HTTP‑01 via `certbot`, with background
auto‑renewal), or **reuse** an existing managed cert across mappings.

**🧩 Snippets — reusable settings, picked per mapping**
One **Snippets** page with a section per kind: **rate limits** (connections per
client IP, per‑connection bandwidth), **timeouts**, **log formats** (custom
`log_format` bodies incl. JSON), **error pages** (uploaded HTML nginx serves via
`error_page`), **config snippets** (raw nginx blocks as `include` files) and
**log rotation** policies. The mapping form has a single **Snippets** picker
instead of per‑mapping fields; custom locations get their own picker for one
path. Nothing selected = built‑in defaults; editing a snippet re‑applies every
mapping that uses it (`nginx -t` guarded), and a snippet in use can't be deleted.

**📜 Logs with history**
Per‑mapping access/error logs, rotated daily by `logrotate` (dated, gzip,
retention per mapping or a global default). The Logs page tails live, and a
**time‑range search** reads the live file *and* every rotated archive — "what
hit this mapping two days ago at 15:40?" — with per‑archive downloads.

**🛑 Access lists (allow/deny)**
Named IP/CIDR allow lists rendered to Nginx snippets; a built‑in **tas‑ix
(Uzbekistan)** list auto‑refreshes from the MRLG looking glass.

**🧱 Per‑interface firewall**
Security‑group‑style `iptables` rules per interface (protocol, port range, source,
action, priority) + default policies. Off by default, with **lockout protection**
and a one‑click **Panic** teardown.

**🛡️ WAF (ModSecurity + OWASP CRS)**
Front the UI — or any Reverse Proxy mapping — with ModSecurity/CRS. Install from
the dashboard; Off / Detection / Enforce modes.

**↪️ Forward proxy**
Standalone outbound HTTP forward proxies, each bound to its own IP:port with an
allow‑all or domain‑pattern allowlist — separate from reverse‑proxy mappings.

**💾 Backup / restore**
Export/import mappings, plus **full‑system snapshots** (everything incl. SSL keys)
as timestamped zips with scheduled auto‑backups and one‑click rollback.

</td>
</tr>
</table>

Plus a **Monitoring page** (CPU/RAM/disk/network from `/proc`, and live
**nginx `stub_status`** counters — active / reading / writing / waiting
connections, requests per second — from a loopback‑only status endpoint Splitter
provisions itself), a **network Tools page** (ping, port test, **port scanner /
nmap**, DNS lookup, traceroute, tcpdump, WHOIS, SSL check), and a **live routing
map** (n8n‑style canvas of every mapping with red/✗ flagging when a backend is
down, plus the same nginx counters in its header). Every option on the mapping
form carries an **ⓘ tooltip** explaining what it does.

---

## 🏗️ Architecture

Each domain can get its **own sub‑interface (unique IP)** on the host. For a
**Stream** mapping, Nginx `stream` listens on that IP and load‑balances across
the domain's backend pool at Layer 4. For a **Reverse Proxy** mapping, Nginx
terminates HTTP(S), optionally runs the request through ModSecurity/CRS, and
proxies it at Layer 7.

<p align="center">
  <img src="docs/architecture.png" alt="Splitter architecture" width="960">
</p>

**Provisioning pipeline** (matches the manual commands you'd otherwise run):

```bash
ip link add link eth0 name eth0.50 type vlan id 50          # if a VLAN ID is given
ip link add link eth0.50 name mv-site4-0 type macvlan mode bridge   # (ipvlan on VMware)
ip addr add 192.168.50.15/24 dev mv-site4-0                 # or: dhclient mv-site4-0
sysctl -w net.ipv4.ip_nonlocal_bind=1                       # bind not-yet-up IPs
nginx -t && nginx -s reload                                 # stop+start if reload won't bind
```

Generated config (Stream):

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

A Docker‑backed backend renders the same way, except the `server` line's
address is resolved from the container's current IP at generation time and kept
current automatically — the stored backend is the **container name**, not the
address.

---

## 🐳 Docker and Swarm backends

This is a Splitter *feature* (a page in the dashboard for picking Docker/Swarm
containers as backends) — not to be confused with [running Splitter itself in
Docker](#-running-in-docker), which is just a packaging option.

Splitter runs in the **host network namespace**, so it can't rely on Docker's
embedded DNS for service names. Instead it talks to the Docker Engine API over
its unix socket to discover running containers (name, network IPs, exposed
ports) — or, on a **Swarm manager**, services (routing‑mesh published port,
replica count) — and turns a chosen one into a concrete backend the host can
reach directly (the host has routes to the docker bridges, so a container's
`172.x` address is reachable from the host netns without publishing ports).

- **Docker page** — its own Stream/Reverse‑Proxy tabs and mapping list, kept
  entirely separate from Map's own tables. "New Stream"/"New Proxy" opens the
  normal mapping form directly; an inline **container grid** inside the
  Backends section lets you check off one or more containers/services as
  backends, right alongside (or instead of) manually‑typed addresses.
- **Self‑healing addresses** — a background reconciler periodically
  re‑resolves every Docker‑backed backend; when a container is recreated (new
  IP), the affected mapping's config is re‑rendered and nginx reloaded
  automatically, no user action needed.
- **Real‑time reaction** — a Docker **events** watcher streams the Engine API's
  event feed and fires an immediate reconcile the moment a container starts,
  stops, dies, or its health check changes (and on Swarm service updates), so a
  dead backend drops out of the pool — and a recovered one rejoins — within a
  fraction of a second. The periodic poll stays on as a safety net and
  reconnect fallback.

---

## 🐳 Running in Docker

The container runs in the **host network namespace** with the kernel privileges
Splitter needs to create real macvlan/VLAN interfaces, bind their IPs in Nginx,
and manage `iptables`. This is the only mode in which the tool works as designed —
Docker here is a **packaging convenience**, not an isolation boundary.

```bash
docker compose up -d --build      # build + start
docker compose logs -f splitter   # watch startup / apply steps
docker compose down               # stop (named volumes keep your data)
```

**What the image bundles**

- Nginx with the **stream** module, iproute2, iptables, dhcp client, openssl, `certbot`, and the diagnostic CLIs.
- **ModSecurity + OWASP CRS** baked in, so the WAF page's **Install** works and survives redeploys (starts off).
- An entrypoint that **auto‑detects the uplink NIC** from the default route, sets `ip_nonlocal_bind`, starts Nginx, and launches the app.

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
> **Runs on a Linux host.** On Docker Desktop (macOS/Windows) there's no real LAN
> NIC in the VM. Two host actions can't work from inside a container: the
> **reboot‑host** button, and — because the macvlan caveats are physics, not
> Docker — some networks (phone hotspots, certain switches) block extra MACs.

---

## 🧭 Using it

1. **Log in** (create the admin on first run).
2. Pick **Stream** or **Reverse Proxy** — the form only shows fields that
   actually apply to the one you picked.
3. **Domain** — `site4.example.com`
4. **Bind target** — a **physical interface** (binds its existing IP), or a **sub‑interface** you created on the Interfaces page (dedicated IP).
5. **Backend pool** — one or more `host:port`, or pick straight from the **Docker container grid**. A second backend auto‑opens the **load‑balancing** options; an optional **rate‑limit** switch adds per‑IP caps.
6. **SSL** — None / Upload / Auto Self‑Signed / Let's Encrypt / Use Existing.
7. **Access list** — global default, none, or a specific list.
8. **Preview** the generated config, or **Save / Apply**.

Each apply shows a step‑by‑step log. The table lists every mapping with Edit /
Delete; the header has Export, Import, and Re‑apply all.

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

---

## ⚙️ Configuration

Everything is env‑overridable, so the same code runs in simulation on a laptop and
live on the host.

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

All `/api/*` routes require an authenticated session except the auth/setup ones.
Each provisioning response includes a `steps[]` array so the UI shows exactly what
happened.

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

## 🔒 Notes &amp; safety

- **Login required** — passwords stored only as salted PBKDF2 hashes; the last admin can't be deleted; sessions are cookie‑only.
- **No shell injection** — every command runs as an argv list, never a shell string.
- **Validated input** — domains (RFC‑1123), IPs, `host:port` backends, MACs, VLAN IDs; domains are path‑sanitised.
- **Fail‑safe reloads** — `nginx -t` before every reload; a bad block is rolled back so the live proxy is never reloaded against a broken config.
- **Atomic writes** for the store; **0600** for private keys.

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

## 📄 License

Released under the [MIT License](LICENSE) — © 2026 Splitter Contributors.
