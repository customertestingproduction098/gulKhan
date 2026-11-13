# Sentinel AI - Retail & Warehouse Monitoring

AI-powered surveillance dashboard with two workflows:

1. **Shop interaction monitoring** - detects customers, evaluates greeting
   transcripts, tracks unattended customers, and logs every interaction as
   successful, missed, or unattended. Owners get daily/weekly PDF reports.
2. **Warehouse after-hours security** - detects people outside working hours,
   emails the owner, escalates unacknowledged critical alerts, and records
   entries with snapshots and false-alarm review.

Built with React + Vite + Tailwind on the frontend and Flask + MongoDB + OpenCV on
the backend. Google Cloud Vision is used when configured, with OpenCV HOG and a
local greeting matcher as fallbacks. Email is sent via Python's built-in smtplib.

---

## Highlights

- **Single RTSP connection per camera** - one `FrameGrabber` feeds the live MJPEG
  preview, snapshots, recent-frame buffer, and analysis loop.
- **Real person detection** - Google Vision object localization or OpenCV HOG.
- **Greeting reasoning** - OpenRouter classifies transcripts; deterministic phrase
  matching is the fallback.
- **Unattended customer detection** - staff-zone, greeting, and persistent-person
  heuristics with a configurable grace timer.
- **Escalation** - unacknowledged critical alerts send a reminder email, then a
  secondary-contact email; acknowledgement stops escalation immediately.
- **Editable settings** - owner contacts, business hours, notification channels,
  escalation delays, monitoring behavior, camera names/enablement, and report
  delivery are stored in MongoDB and editable from the UI.
- **Reports** - daily, weekly, and monthly summaries, CSV/PDF export, email
  delivery, and scheduled daily/weekly reports.
- **Real-time UI** - Server-Sent Events (`/api/events`) push alerts and detection
  events to the dashboard, with polling fallback.
- **Graceful degradation** - alerts are always persisted; email, push, and Vision
  report `skipped`, `failed`, or fallback when not configured.

---

## Project structure

```
surveillance-system/
|-- frontend/                     # React 19 + Vite + Tailwind
|   |-- src/
|   |   |-- components/           # Navigation, Dashboard, CameraViewer, AlertLog
|   |   |-- hooks/                # useEventStream (SSE)
|   |   |-- pages/                # Cameras, Shop, Warehouse, Alerts, Reports, Settings
|   |   |-- store/                # Redux store + camera/alert/auth slices
|   |   |-- services/             # Axios API client
|   |   |-- utils/                # Date/format/URL helpers
|   |   |-- App.jsx
|   |   |-- main.jsx
|   |   `-- index.css
|   |-- .env.example
|   |-- tailwind.config.js
|   `-- package.json
|-- backend/                      # Flask API
|   |-- config/                   # settings.py (all env vars)
|   |-- models/                   # Interaction, Entry, Alert, DailyReport
|   |-- routes/                   # camera, interactions, entries, alerts, reports, settings, events, logs
|   |-- services/
|   |   |-- camera_processor.py   # FrameGrabber, CameraManager, SurveillanceProcessor
|   |   |-- google_apis.py        # Cloud Vision person detection
|   |   |-- openrouter.py         # Greeting reasoning
|   |   |-- alert_system.py       # Email / push / alert persistence
|   |   |-- escalation.py         # Unacknowledged critical alert escalation
|   |   |-- reports.py            # Summaries, CSV, PDF
|   |   |-- report_scheduler.py   # Scheduled daily/weekly delivery
|   |   |-- settings_service.py   # MongoDB-backed editable settings
|   |   |-- events.py             # In-process event broker
|   |   `-- mongodb_handler.py    # Repository + aggregations
|   |-- utils/                    # Shared helpers
|   |-- static/snapshots/         # Evidence images (git-ignored)
|   |-- app.py
|   |-- requirements.txt
|   `-- .env.example
|-- email-service/                # Node.js + Nodemailer SMTP bridge
|   |-- server.js
|   |-- package.json
|   `-- .env.example
`-- README.md
```

---

## Quick start

### Prerequisites

- Node.js 20+ and npm
- Python 3.10+
- MongoDB running locally or a MongoDB Atlas URI
- An RTSP-capable camera/NVR (optional for UI-only work)
- SMTP credentials for the email service (optional)

### 1. Backend

```bash
cd backend

# Windows
python -m venv venv
venv\Scripts\activate

# macOS / Linux
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt

# Create your environment file
copy .env.example .env       # Windows
cp .env.example .env         # macOS / Linux
```

Edit `backend/.env`:

```ini
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=surveillance

CAMERA_HOST=192.168.1.50
CAMERA_PORT=554
CAMERA_USER=admin
CAMERA_PASSWORD=your-password
CAMERA_CHANNELS=16

OWNER_EMAIL=owner@example.com
SMTP_HOST=smtp.example.com
MAIL_FROM=alerts@example.com
```

Run the API:

```bash
python app.py
# http://localhost:5000/api/health
```

MongoDB collections and indexes are created automatically. The app writes to
`shop_interactions`, `warehouse_entries`, `alerts`, `daily_reports`, `settings`,
`report_runs`, and `logs`.

### 2. Frontend

```bash
cd frontend
npm install
copy .env.example .env       # optional; defaults to http://localhost:5000/api
npm run dev
# http://localhost:5173
```

Sign in with any owner name (local demo session) to open the dashboard.

### 3. Try it

1. Open **Cameras** to see the multi-camera grid and start monitoring.
2. Open **Shop Floor**, select a camera, and click **Start**.
3. Send a transcript to exercise greeting detection (no microphone needed):
   ```bash
   curl -X POST http://localhost:5000/api/camera/audio \
     -H "Content-Type: application/json" \
     -d "{\"transcript\":\"Good morning, welcome!\",\"confidence\":0.9}"
   ```
4. Open **Warehouse**, start monitoring, and use **Send alert**.
5. Open **Alerts** to review, open, and acknowledge incidents.
6. Open **Reports** for daily/weekly/monthly views, PDF/CSV export, and email.
7. Open **Settings** to edit hours, contacts, escalation, monitoring, cameras,
   and report delivery.

---

## Configuration reference

All values live in `backend/.env`; see `.env.example` for the full list. Settings
edited in the UI are stored in MongoDB and override these environment defaults.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MONGODB_URI` / `MONGODB_DB` | `mongodb://localhost:27017` / `surveillance` | Database connection |
| `CAMERA_HOST`, `CAMERA_PORT`, `CAMERA_USER`, `CAMERA_PASSWORD` | - | NVR/RTSP camera |
| `CAMERA_CHANNELS` | `16` | Channels shown in the UI |
| `CAMERA_STREAM_URL` | - | Overrides the templated RTSP URL |
| `CAMERA_RTSP_TEMPLATE` | Dahua-style path | Custom RTSP path |
| `ANALYSIS_INTERVAL_SECONDS` | `3` | Frame sampling interval |
| `ANALYSIS_WIDTH` | `640` | Width used for analysis frames |
| `FRAME_BUFFER_SECONDS` | `5` | Recent-frame buffer for evidence snapshots |
| `SHOP_OPEN_TIME` / `SHOP_CLOSE_TIME` | `06:00` / `21:00` | Shop hours |
| `WAREHOUSE_AFTER_HOURS_START` / `_END` | `21:00` / `06:00` | After-hours window (crosses midnight) |
| `UNATTENDED_DETECTION_ENABLED` | `false` | Enable unattended shop alerts |
| `UNATTENDED_ALERT_SECONDS` | `10` | Grace period before an unattended alert |
| `STAFF_PRESENCE_SECONDS` | `20` | Persistent-person staff heuristic |
| `SHOPKEEPER_ZONE` | - | Normalized staff box `x1,y1,x2,y2` |
| `BACKGROUND_JOBS_ENABLED` | `true` | Start escalation and report schedulers |
| `GOOGLE_APPLICATION_CREDENTIALS` | - | Service-account JSON for Vision/push |
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | - / `openai/gpt-4o-mini` | Greeting reasoning |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURE`, `SMTP_USER`, `SMTP_PASS` | - | SMTP transport |
| `MAIL_FROM` | - | Sender address |
| `OWNER_EMAIL`, `SECONDARY_EMAIL` | - | Alert and report recipients |
| `ESCALATION_EMAIL_AFTER`, `ESCALATION_SECONDARY_AFTER` | `300` / `900` | Reminder / secondary delays |
| `FIREBASE_PROJECT_ID`, `FIREBASE_DEVICE_TOKEN` | - | Optional push notifications |
| `DAILY_REPORT_ENABLED`, `DAILY_REPORT_TIME` | `true`, `21:00` | Daily report schedule |
| `WEEKLY_REPORT_ENABLED`, `WEEKLY_REPORT_DAY` | `true`, `monday` | Weekly report schedule |
| `CORS_ORIGINS` | local Vite origins | Comma-separated allowed origins |
| `PUBLIC_BASE_URL` | `http://localhost:5000` | Absolute snapshot links |
| `VITE_API_URL` (frontend) | `http://localhost:5000/api` | API base URL |

Optional integrations are independent: the app runs without Google, OpenRouter,
SMTP, or Firebase and reports active features in `/api/health`.

---

## API

Base path: `/api`

| Method | Endpoint | Description |
| --- | --- | --- |
| GET | `/health` | API, MongoDB, features, jobs, and live-event status |
| GET | `/camera/status` | Monitoring state, camera stats, hours, features |
| GET | `/camera/<channel>/status` | Individual camera status |
| GET | `/camera/stream/<channel>` | MJPEG live preview |
| GET | `/camera/snapshot/<channel>` | Single JPEG frame |
| POST | `/camera/monitor/start` | Body: `{ "channel": 1, "mode": "shop" \| "warehouse" }` |
| POST | `/camera/monitor/stop` | Stop the analysis loop |
| POST | `/camera/<channel>/process` | Start a channel directly |
| POST | `/camera/<channel>/inspect` | Analyze one frame and return detections |
| POST | `/camera/audio` | Body: `{ "transcript": "...", "confidence": 0.0 }` |
| GET | `/interactions?limit=&days=&type=&channel=&start=&end=` | Shop interactions |
| GET | `/interactions/stats?days=7` | Interaction aggregates |
| GET | `/interactions/<id>` | Interaction detail |
| GET | `/entries?limit=&days=&channel=&after_hours=&false_alarm=` | Warehouse entries |
| GET | `/entries/stats?days=7` | Entry aggregates |
| GET | `/entries/<id>` | Entry detail |
| POST | `/entries/<id>/false-alarm` | Body: `{ "false_alarm": true }` |
| GET | `/alerts?limit=&days=&severity=&type=&channel=&status=` | Alert history |
| GET | `/alerts/pending` | Unacknowledged alerts |
| GET | `/alerts/<id>` | Alert detail with escalation history |
| POST | `/alerts/<id>/acknowledge` | Mark handled and stop escalation |
| POST | `/alerts/send` | Create and optionally deliver an alert |
| GET | `/settings` | Editable settings plus active feature flags |
| PUT/PATCH | `/settings` | Update settings (raw or wrapped in `settings`) |
| GET | `/reports/summary?days=7` | Aggregated summary |
| GET | `/reports/daily?date=YYYY-MM-DD` | Daily report |
| GET | `/reports/weekly?week=YYYY-Www` | Weekly report |
| GET | `/reports/monthly?month=YYYY-MM` | Monthly report |
| GET | `/reports/history?limit=30` | Persisted daily reports |
| GET | `/reports/export/csv?days=7` | CSV export |
| GET | `/reports/export/pdf?days=7` | PDF export |
| POST | `/reports/send` | Body: `{ "kind": "daily" \| "weekly", "value": "..." }` |
| GET | `/events` | Server-Sent Events stream |
| GET | `/events/status` | Subscriber and publish counters |
| WS | `/api/events/ws` | Optional WebSocket stream (`flask-sock`) |
| GET | `/logs/*` | Backward-compatible aliases for interactions, entries, reports |

### Live events

`GET /api/events` emits named SSE events: `interaction.created`, `entry.created`,
`entry.updated`, `alert.created`, `alert.acknowledged`, `alert.escalated`,
`settings.updated`, `monitoring.started`, and `monitoring.stopped`.

---

## How detection works

### Shop greeting flow

1. The analysis loop samples frames and runs person detection.
2. When at least one person is visible, an interaction window opens.
3. A transcript is posted to `POST /api/camera/audio` by an external transcriber.
4. OpenRouter (or the local phrase matcher) decides whether it contains a greeting.
5. The interaction is saved with type `SUCCESSFUL`, `MISSED`, or `UNATTENDED`,
   along with customer count, response time, confidence, transcript, and snapshot.
6. If unattended detection is enabled, a grace timer raises an alert when
   customers wait with no staff present.

Staff presence is inferred from `SHOPKEEPER_ZONE` when configured, otherwise from
greeting activity and persistent-person heuristics.

### Warehouse flow

1. A person appearing after hours creates an entry with `after_hours=True`.
2. An alert is persisted, then email/push delivery is attempted.
3. Critical alerts escalate through a reminder email and a secondary contact
   until acknowledged.
4. A snapshot is stored under `backend/static/snapshots/` and linked in the alert.
5. Owners can mark an entry as a false alarm from the UI.

---

## Migration note

Version 2.0 writes to the new `shop_interactions`, `warehouse_entries`, `alerts`,
and `daily_reports` collections. Records created by the earlier demo build in the
legacy `interactions` and `entries` collections are intentionally not shown. To
start fresh, use a new `MONGODB_DB` value; to keep old data, migrate it into the
new schemas before pointing the UI at the database.

If you previously committed camera credentials in the legacy root script, rotate
the NVR password and keep `backend/.env` out of version control.

---

## Deployment (Render)

### Backend - Web Service

- Root directory: `backend`
- Python version: `3.12.7` (pinned in `backend/.python-version`)
- Build command: `pip install --upgrade --no-cache-dir -r requirements.txt`
- Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16 --timeout 600 --graceful-timeout 30 --keep-alive 75`
- Health check path: `/api/health`
- Add all `backend/.env` values as environment variables.
- Keep **one worker**: camera connections, analysis, escalation, and schedulers
  live in the process.
- Set `CORS_ORIGINS` to the frontend URL and `PUBLIC_BASE_URL` to the backend URL.

### Frontend - Static Site

- Root directory: `frontend`
- Build command: `npm ci && npm run build`
- Publish directory: `dist`
- Set `VITE_API_URL=https://<your-backend>.onrender.com/api`.

### Keeping the free instance awake

Render stops free web services after ~15 minutes without inbound HTTP traffic,
and the next request can take 30-60s to boot. A scheduler running inside the
backend cannot prevent this (it sleeps with the service), so the ping must come
from an external runner.

This repo includes `scripts/keep_alive.py` and the scheduled workflow
`.github/workflows/keep-alive.yml`, which requests `/api/health` every 5 minutes
(with retries that tolerate a cold start). Push to the default branch (`main`)
and enable Actions if it is disabled; scheduled workflows only run from the
default branch.

Alternatives: a free uptime monitor (UptimeRobot, cron-job.org) pinging the same
URL every 5-10 minutes, or a paid Render instance (no spin-down). One always-on
free service uses roughly 730 of the 750 free instance hours per month.

> RTSP cameras are usually on a private LAN. For cloud deployments, expose the
> NVR through a secure tunnel/VPN or run the backend on-premises. Never expose an
> NVR directly to the public internet.
