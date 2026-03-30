# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running locally

```bash
cd logistic_pro_web
pip install -r requirements.txt
python app.py
# → http://localhost:5002
```

## Deploying to Vercel

```bash
cd logistic_pro_web
git add -A && git commit -m "..."
vercel --prod --yes
```

Live URL: **https://logisticproweb.vercel.app**

## Architecture

### Backend — `app.py` (Flask, single file)

- Supports **SQLite** (local dev) or **PostgreSQL** (production) via the `DATABASE_URL` env var.
- When `DATABASE_URL` is set, `psycopg2` is used with `RealDictCursor`; otherwise SQLite at `./logisticpro.db`.
- All SQL is written twice — once with `%s` placeholders (Postgres) and once with `?` placeholders (SQLite), selected via `USE_PG` flag.
- All mutating endpoints (`POST`, `PUT`) return `{"success": true, "record": {...}}` with the full updated row so the frontend can update state without refetching.
- `awb_no` has a `UNIQUE` constraint enforced at both DB level and in endpoint logic before any write.
- File uploads saved to `./uploads/` locally or `/tmp/lp_uploads/` on Vercel (ephemeral). Served via `/uploads/<filename>`.
- `/sw.js` and `/manifest.json` are served from `static/` for PWA support.

### Frontend — `templates/index.html` (single-file SPA)

No build step. Plain HTML/CSS/JS with Chart.js 4.4.0 and chartjs-plugin-datalabels.

**State variables:**
- `allRows` — master data array (source of truth for the table)
- `selectedId` — row currently loaded in the form for edit/delete
- `filterStatus`, `filterMonth` — active dashboard → shipment filters
- `monthlyData` — raw monthly array kept in sync for chart click reference
- `awbObjectUrl`, `invObjectUrl` — blob URLs for newly selected (unsaved) files
- `awbSavedFile`, `invSavedFile` — filenames of previously saved attachments

**Data flow:**
- `loadData()` fetches from server, falls back to `localStorage` (`lp_v1`) on network failure.
- Server is always authoritative — response always overwrites the cache.
- `saveLocal()` syncs `localStorage` after every mutation and updates the sidebar shipment count badge.
- Mutations never call `loadData()` — they patch `allRows` directly then call `renderTable()` + `updateDashboard()`.

**Key patterns:**
- `validate()` checks AWB uniqueness against `allRows` on the frontend before hitting the server (fast feedback). The server also checks before writing.
- `selectedId` must be captured into `const sid = selectedId` at the top of `updateRecord()` and `deleteRecord()` before any form reset that nulls it.
- Chart clicks call `goFilter('status', label)` or `goFilter('month', 'YYYY-MM')` which navigates to Shipment and filters the table.
- Dashboard stat cards also call `goFilter` on click.

**Mobile / responsive:**
- Desktop (>900px): fixed 256px sidebar, 4-col stat cards, side-by-side charts.
- Mobile (≤900px): sidebar slides in via hamburger, fixed bottom nav bar, charts stacked, form slides up as a bottom sheet.
- Mobile (≤480px): 1-col form fields, full-width stacked buttons, `#` column hidden in table.
- `env(safe-area-inset-bottom)` applied to bottom nav and form sheet for notched phones.

**PWA:**
- Service worker at `static/sw.js` — network-first for all requests, API calls bypass cache.
- Manifest at `static/manifest.json` with SVG icon.
- Registered in JS at page load via `navigator.serviceWorker.register('/sw.js')`.

## Database (Neon PostgreSQL — production)

- Project: `logistic-pro` on Neon free tier (org: `org-late-tooth-21034841`)
- `DATABASE_URL` is set in Vercel environment variables (production only).
- The `shipments` table is auto-created by `init_db()` on first request.
- Schema: `id, date, awb_no (UNIQUE), cost, status, awb_file, invoice_file, created_at`.

To get the connection string locally for testing:
```bash
npx neonctl connection-string --project-id ancient-lab-11112991
```

## Key API routes

| Method | Path | Notes |
|---|---|---|
| GET | `/api/shipments` | Returns all rows ordered by `created_at DESC` |
| POST | `/api/shipments` | Checks AWB uniqueness, returns `{success, record}` |
| PUT | `/api/shipments/<id>` | Checks AWB uniqueness excluding self, returns `{success, record}` |
| DELETE | `/api/shipments/<id>` | Returns `{success}` |
| GET | `/api/dashboard` | Returns `{total, transit, delivered, returned, monthly[]}` |
| GET | `/uploads/<filename>` | Serves uploaded files |
| GET | `/sw.js` | Service worker |
| GET | `/manifest.json` | PWA manifest |

## Template sync rule

`logistic_pro_web/templates/index.html` is the deployed file.
`AI_Agent/logisticpro_templates/index.html` is the local copy used by `logisticpro_app.py` (port 5002).
They must stay in sync — the only difference is `/uploads/` vs `/lp_uploads/` in `previewFile()`.
