

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
2. Copy the provider's **Client ID**, **Client Secret** and **OpenID
   Configuration Issuer** into `.env`:
   ```
   OIDC_ISSUER=https://auth.example.com/application/o/devicemanager/
   OIDC_CLIENT_ID=...
   OIDC_CLIENT_SECRET=...
   PUBLIC_URL=https://dm.example.com
   SESSION_HTTPS_ONLY=true
   ```
3. Restrict who gets in: on the application's **Policy / Group / User
   Bindings** tab, bind the group allowed to use it (authentik then refuses
   everyone else). `OIDC_ALLOWED_GROUP=<group>` adds the same check here.
4. `docker compose up -d`, open the dashboard's **Users** page: the Sign-in
   panel should say **ready**. Sign in with authentik in a private window.
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
