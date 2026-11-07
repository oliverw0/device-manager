

# DeviceManager — Host

<img width="1123" height="724" alt="image" src="https://github.com/user-attachments/assets/ea5efc90-c0aa-4108-8c8d-cdbd9bbff7cd" />

Self-hosted monitoring dashboard and notifier for a small fleet of machines,
VMs, LXCs and containers. Clients push periodic reports; the host stores them,
shows a live dashboard, and sends an ntfy alert when a device stops reporting.

Companion agent: [device-manager-client](https://github.com/oliverw0/device-manager-client).

## What it does

- Live dashboard: per-device CPU / memory / disk, uptime, Tailscale status,
  SSH auth activity, and Docker containers (grouped by Compose stack).
- Offline detection: alerts via ntfy when a device misses reports, and again
  when it recovers. Also alerts on Tailscale connect/disconnect transitions.
- Per-container CPU/memory history sparklines and a per-device history chart.
- In-browser SSH terminal and Docker container controls (logs, start, stop,
  restart) over the host's own key, tried Tailscale-first then local IP.
- Linux and Windows clients (desktop and Server). Windows devices get a
  PowerShell console, Windows Update in place of apt, and the same container
  controls; the host picks the commands from the OS each client reports.

## Monitoring features

- **Checks**: HTTP (optionally expecting a status or page text), TCP, DNS
  (asks one server for a name), heartbeats for cron jobs, **PBS** (failed
  tasks, no backups in 24h, each guest's newest backup age, datastore space) and
  **Proxmox VE** (storage and thin-pool metadata, SMART health, guests set to
  start at boot that are stopped). Each PBS/Proxmox finding is its own problem.
- **Sonarr / Radarr / Lidarr queue** (API key): downloads the torrent client
  refused (bad file type, finished at 0 B: fake releases) and ones that
  finished but didn't import. One problem per show or film, naming its
  episodes; new ones go out as one short push ("Slow Horses failed to
  download" / "S06E01–E03 · Bad file type (.exe)") with a **Remove & search
  again** button that blocklists the release and searches for another. Pressed
  on the Health page, a progress card follows that search (searching,
  downloading with a percentage and time left, imported / nothing found / fake
  again); pressed on the phone, one short push says how it ended. A Downloads
  section in the daily summary.
- **Health** (the home page): what needs attention now, every check grouped (DNS, Proxmox,
  Backups, or your own groups) with 30-day uptime, each opening to show what it
  found; a **Timeline** of the last 24h / 7d / 30d; and **Sent alerts** (every
  ntfy push, whether it was delivered, watchdog state). Optional read-only
  public status page.
- **Map** of what runs where (LXCs under their Proxmox host).
- **Alerts that don't pile up**: a guest whose host is down is named in the
  host's push; when most machines go quiet at once, one "lost contact" push.
  Unhealthy and restart-looping containers, SSH logins from outside your
  networks and bursts of failed logins also alert. Unacknowledged high
  problems get a reminder every `REMINDER_HOURS`.
- **Acknowledge / Mute 1h buttons** on the phone notification (needs `PUBLIC_URL`).
- **Watchdog**: pings `WATCHDOG_URL` (healthchecks.io etc.) while Pulse is
  sweeping and ntfy works, so an outside service notices when Pulse stops.
- Per-device alert thresholds and "runs on" on each device's Settings tab;
  hourly history kept for a year (7d / 30d / 90d / 1y charts).
- `/metrics` (Prometheus) and `/api/v1/summary` (Homepage widget), both behind
  `METRICS_TOKEN`.

## Requirements

- Docker and Docker Compose v2.
- An ntfy topic (self-hosted or ntfy.sh) for alerts — optional; leave blank to
  disable notifications.

## Setup

```
git clone https://github.com/oliverw0/device-manager && cd device-manager
cp .env.example .env
# edit .env (at minimum set ADMIN_PASSWORD and NTFY_URL)
docker compose up -d --build
```

The dashboard is served on port 8000. Log in with the admin credentials from
`.env`. Data (SQLite database and the generated SSH key) persists in `./data`.

## Configuration (.env)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ADMIN_USERNAME` | `admin` | Dashboard login user. |
| `ADMIN_PASSWORD` | `change-me` | Dashboard login password. See the `$` note below. |
| `NTFY_URL` | (empty) | Full ntfy topic URL, e.g. `https://ntfy.sh/my-topic`. Empty disables alerts. |
| `NTFY_DEFAULT_PRIORITY` | `default` | ntfy priority for notifications. |
| `OIDC_ISSUER` | (empty) | OpenID Connect issuer URL, e.g. authentik's `https://auth.example.com/application/o/devicemanager/`. Set with `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` to add "Sign in with authentik". |
| `OIDC_AUTO_CREATE` | `true` | Create a user on their first OIDC sign-in. |
| `OIDC_ALLOWED_GROUP` | (empty) | Only members of this group (the `groups` claim) may sign in. |
| `OIDC_NAME` | `authentik` | Name on the sign-in button. |
| `PUBLIC_URL` | (empty) | The dashboard's address as browsers see it; behind a proxy it sets the OIDC redirect URI (`<PUBLIC_URL>/login/oidc/callback`). |
| `PASSWORD_LOGIN` | `true` | Set `false` to hide the username/password form once OIDC works. |
| `SESSION_SECRET` | (empty) | Login cookie signing key. Left empty, one is generated and persisted in `./data`. |
| `SESSION_HTTPS_ONLY` | `false` | Mark the login cookie `Secure`. Set `true` only when reached over HTTPS; over plain HTTP it breaks login. |
| `DATABASE_URL` | SQLite in `./data` | Point at Postgres if you outgrow SQLite. |
| `CHECK_INTERVAL_SECONDS` | `15` | How often the host checks for devices that went silent. |
| `HISTORY_RETENTION_DAYS` | `7` | How long per-report history is kept. |
| `LONG_HISTORY_DAYS` | `400` | How long hourly averages are kept (30d / 90d / 1y charts). |
| `ALERT_CPU_PERCENT` / `ALERT_MEM_PERCENT` / `ALERT_DISK_PERCENT` | `95` / `90` / `90` | Default thresholds; each device can override them. |
| `REMINDER_HOURS` | `8` | Re-push unacknowledged high problems this often. `0` = off. |
| `WATCHDOG_URL` | (empty) | Called after every healthy sweep (at most every `WATCHDOG_INTERVAL_SECONDS`, 60), e.g. a healthchecks.io ping URL. |
| `STORM_MIN_DEVICES` | `3` | Machines going quiet at once (and most of the fleet) that count as Pulse's own network. |
| `SSH_FAILED_ALERT` | `20` | Failed SSH logins in one report window that alert. `0` = off. |
| `SSH_OUTSIDE_LOGIN_ALERT` | `true` | Alert on a successful SSH login from outside private, Tailscale and loopback ranges. |
| `PBS_BACKUP_MAX_AGE_HOURS` | `26` | A guest's newest PBS backup older than this is late. |
| `PBS_RETIRED_DAYS` | `14` | Backup groups silent longer than this are taken as removed guests. |
| `METRICS_TOKEN` | (empty) | Bearer token for `/metrics` and `/api/v1/summary`. Empty turns both off. |
| `PUBLIC_STATUS` | `false` | Serve a read-only status page at `/status/public`, outside the login. |

Passwords containing `$`: Docker Compose interpolates `${...}` in `.env`, so a
`$` in `ADMIN_PASSWORD` is eaten. Escape it as `$$`, or put the password in a
plain file and set `ADMIN_PASSWORD_FILE=/run/secrets/admin_password` (see the
commented mount in `docker-compose.yml`).

## Sign in with authentik

1. In authentik: **Applications > Applications > Create with provider**.
   - Name `DeviceManager`, slug `devicemanager`.
   - Provider: **OAuth2/OpenID Connect**, client type **Confidential**,
     authorization flow `default-provider-authorization-implicit-consent`.
   - Redirect URI (strict): `https://<your dashboard>/login/oidc/callback`.
     The dashboard's Users page shows the exact URI it uses.
   - Leave the scopes at the defaults (`openid`, `email`, `profile`: profile
     carries `groups`).
2. On the dashboard's **Users** page, fill in **Sign in with authentik**: the
   provider's **OpenID Configuration Issuer**, **Client ID** and **Client
   Secret** (and the dashboard's public URL if it's behind a proxy). It checks
   the issuer before saving. The same values can go in `.env` instead
   (`OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `PUBLIC_URL`); what's
   saved on the page wins. Behind HTTPS also set `SESSION_HTTPS_ONLY=true`.
3. Restrict who gets in: on the application's **Policy / Group / User
   Bindings** tab, bind the group allowed to use it (authentik then refuses
   everyone else). `OIDC_ALLOWED_GROUP=<group>` adds the same check here.
4. Sign in with authentik in a private window.
5. Once that works, switch off the password account on the Users page (or set
   `PASSWORD_LOGIN=false` to hide the form).

Locked out (password account off and authentik down)?

```
docker compose exec host python -m app.routers.users enable admin
```

## Adding a device

1. Open the dashboard, add a device — it generates an API key.
2. On the device's page, copy the install command (host URL and API key are
   prefilled) and run it on the target machine — a shell command for Linux, or
   a PowerShell one-liner (run as Administrator) for Windows.

## Behind a reverse proxy

The dashboard has no TLS of its own. If you expose it beyond a trusted network,
put it behind a reverse proxy that terminates TLS. Client reports and the
`/api/v1` endpoints must remain reachable.

## Updating

```
git pull
docker compose up -d --build
```

`--build` is required — recreating the container without it keeps the old image.
The SQLite schema migrates itself on start; the `./data` volume is preserved.
