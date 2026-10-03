# TG BOT HOSTER — Hardened HA Edition

A Telegram bot-hosting control bot with admin approval, Python/Node project management, a Vercel cluster dashboard, and MongoDB-backed high-availability state.

## What changed in this hardened build

- Fixed the dashboard/API contract, including the missing `/api/stats` endpoint.
- Added explicit CORS handling for the Vercel dashboard and protected admin API routes with `X-API-Key`.
- Dashboard status now exposes the fields the frontend actually consumes (`leader_since`, `lease_expires_in`, `errors`, etc.).
- HA lease uncertainty now fails closed instead of allowing a second Telegram poller during a MongoDB outage.
- Added MongoDB/GridFS shared snapshots for the SQLite application state, project files, and pending ZIP workflow. Only the current leader writes snapshots; standbys restore the newest state before startup.
- Fixed the legacy SQLite migration so `file_name` is actually migrated to `project_name`.
- Bot lock state is now persisted in the database.
- User Python dependencies install into a project-local `.venv`; they are never installed into the host Python environment.
- Node dependencies are installed locally with lifecycle scripts disabled.
- Added resource controls for user processes: CPU, address-space memory, file size, open files, process count, project disk usage, and bounded logs.
- Added process-group cleanup on POSIX systems.
- Hardened ZIP validation against path traversal, symlinks, excessive entry counts, and decompression bombs.
- Hardened uploaded filenames and ZIP main-file paths.
- Added requirements/package manifest validation to reject URL/path installer directives.
- Removed bundled production `.env` files from the release archive.
- Added deployment-safe `.env.example` files and an expanded 4-instance Render blueprint.

## Important security model

The source scanner is only an additional review signal. It is **not** a sandbox and must never be treated as one.

The hoster now uses project-local environments and OS resource limits, but truly hostile multi-tenant code should still run in a dedicated container/VM/worker account with a restrictive network and filesystem policy. Do not run arbitrary untrusted code as root.

## Required production secrets

Set these in Render (never in source control):

- `BOT_TOKEN`
- `OWNER_ID`
- `ADMIN_ID`
- `MONGO_URI`
- `DASHBOARD_API_KEY` — use a random value of at least 32 characters
- `API_CORS_ORIGINS` — the exact Vercel dashboard origin, e.g. `https://your-dashboard.vercel.app`

If the previous archive was ever uploaded to a repository, shared publicly, or exposed to another person, rotate the Telegram bot token, MongoDB credentials, and dashboard API key before using this release.

## HA deployment

Use `render.hoster.yaml` to create four web services:

- `tg-hoster-1`
- `tg-hoster-2`
- `tg-hoster-3`
- `tg-hoster-4`

All four use the same bot token and MongoDB database. Only one instance owns the Telegram polling lease at a time.

MongoDB is also used for shared snapshots so a standby can recover application/project state after a leader failure. The snapshot worker skips unchanged state and retains only the newest generations.

## Dashboard

The `frontend/` directory is a static dashboard suitable for Vercel.

Configure each Render URL in the dashboard's **Manage** panel. Enter the shared `DASHBOARD_API_KEY` there. The key is stored only in the browser's local storage and is not included in the repository.

## Local run

1. Copy `.env.example` to `.env` and fill in local secrets.
2. Install `requirements.txt`.
3. Run `python main.py`.

For production, prefer the HA configuration and do not disable Mongo-backed state synchronization.

## v3.1 distributed worker bridge

This release adds a control-plane/worker architecture. Telegram polling remains on the bot hoster instances while hosted projects can be dispatched to registered worker services. The Vercel dashboard now includes runtime settings, worker token creation, worker status, worker revocation, and recent job visibility.

### Runtime settings

After the first bootstrap, most operational settings can be changed from **Dashboard → Manage → Runtime configuration** and are stored in the hoster's persistent database. This includes project limits, resource limits, HA timing, CORS, owner/admin IDs, update channel, bot token, and dashboard API key. Secret values are masked in the dashboard.

A small bootstrap environment is still required for a brand-new deployment (database/Mongo connection and the first credentials). A frontend cannot remotely configure a service that has not started yet.

### Distributed workers

Use `render.worker.yaml` as a worker deployment template. Generate a worker token from the dashboard, then bootstrap the new worker once with `BRIDGE_URL` and `WORKER_TOKEN`. After registration, workers are visible in the dashboard and project start/stop jobs can be dispatched through the bridge.

Workers are for legitimate distributed execution and must remain within the resource limits and acceptable-use rules of the deployment provider. This system is not intended to circumvent provider quotas.

## v3.2.1 hardened multi-hoster deployment

This release preserves the four-host HA topology (`tg-hoster-1` through `tg-hoster-4`). Each host is a separate Render web service and uses the same MongoDB lease/state backend. `render.hoster.yaml` explicitly marks all four hosters as `plan: free`.

Important Render Free limitation: Free web services are separate services, but Free usage is subject to the workspace's monthly free-instance-hour allowance and services may spin down when idle. Four Free services therefore do **not** provide guaranteed 24/7 four-node availability at zero cost. Keep this limitation in mind when using HA for production.

### Hardening included

- Revoked worker credentials are rejected immediately.
- Worker bootstrap tokens are single-use and expire.
- Worker jobs/results are bound to the authenticated worker.
- Project bundle downloads require worker/project assignment.
- Project path traversal is rejected before filesystem access.
- Worker process groups are terminated together.
- Worker CPU, memory, process-count and open-file limits are applied where supported.
- Worker job recovery handles stale running jobs.
- Project desired running/stopped state survives HA failover.
- Worker assignment is persisted in MongoDB.
- Runtime numeric settings are refreshed correctly after restart.
- Owner/admin identity is no longer mutable through ordinary runtime settings.
- Dashboard control requests prefer the currently detected HA leader.
- Four Render Free hoster services are retained.

### Execution security

The host process executes uploaded Python/Node programs as OS processes. Resource limits and static scanning are defense-in-depth, not a hostile-code sandbox. Do not expose arbitrary untrusted tenant execution on the same host as the control plane without container/VM isolation.
