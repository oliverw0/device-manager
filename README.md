# DeviceManager — Host

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
| `SESSION_SECRET` | (empty) | Login cookie signing key. Left empty, one is generated and persisted in `./data`. |
| `DATABASE_URL` | SQLite in `./data` | Point at Postgres if you outgrow SQLite. |
| `CHECK_INTERVAL_SECONDS` | `15` | How often the host checks for devices that went silent. |
| `HISTORY_RETENTION_DAYS` | `7` | How long per-report history is kept. |

Passwords containing `$`: Docker Compose interpolates `${...}` in `.env`, so a
`$` in `ADMIN_PASSWORD` is eaten. Escape it as `$$`, or put the password in a
plain file and set `ADMIN_PASSWORD_FILE=/run/secrets/admin_password` (see the
commented mount in `docker-compose.yml`).

## Adding a device

1. Open the dashboard, add a device — it generates an API key.
2. On the device's page, copy the install command (host URL and API key are
   prefilled) and run it on the target machine.

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
