# S.A.M.S. — Smart Audio Monitoring System

Privacy-aware bullying detection for school zones where cameras can't go.
An edge device uploads a short audio clip to Supabase Storage and notifies the
cloud backend, which runs scream detection, transcribes the speech (Groq
Whisper), runs an NLP threat classifier, and — if a scream is detected **or**
the threat score crosses the threshold — raises an alert that is pushed live to
a **central web dashboard** (disciplinary staff) and a **teacher phone app**
(PWA). All staff-facing APIs are protected by JWT login (FR23).

**Author:** Lim Xin Hui · BMCS3403 Project I · TARUMT 2025/26
**Scope (this repo):** Speech Detection & Audio Capture · Cloud Processing & AI Analysis · Reporting & Analytics · Main Computer Monitoring System

---

## 1. What's in this repo

```
sams_final/
├── sams_backend/                FastAPI cloud backend (capture → STT → NLP → alert → push)
│   ├── main.py                  App entry point + WebSocket + /m PWA mount
│   ├── requirements.txt
│   ├── .env.example             Copy to .env and configure
│   ├── simulate_edge.py         Stand-in for the ESP32 device (OUTDATED — see §11)
│   ├── api/                     auth.py (JWT login) · events.py · alerts.py · analytics.py · reports.py · admin.py · dependencies.py
│   ├── services/                stt · nlp · ser · mqtt · audio_capture · processing_pipeline · websocket_manager · storage
│   ├── models/                  database.py (ORM/ERD) · schemas.py (Pydantic)
│   ├── utils/                   auth.py (bcrypt) · seed_db.py (demo data)
│   ├── config/                  settings.py (env config)
│   └── tests/                   pytest suite
├── sams_dashboard/              Central monitoring dashboard (web, for disciplinary staff)
│   └── index.html
├── sams_mobile/                 Teacher PWA — phone app that receives alerts (served at /m)
│   ├── index.html · app.js · styles.css
│   ├── manifest.webmanifest · service-worker.js
│   └── icons/
└── README.md                    You are here
```

The three pieces talk over one backend:

```
 Edge device (ESP32-C3)
   1. uploads WAV ──────────►  Supabase Storage (audio-clips bucket)
   2. notifies    ──HTTP───►  POST /api/events/audio  (supabase_file_path)
                                  │  download clip → scream detection (TFLite)
                                  │  → Whisper STT → NLP threat score
                                  │  → SER emotion (wav2vec2) — angry speech
                                  │    boosts the threat score before the decision
                                  │  store Event/AudioClip/Transcript/Analysis/EmotionAnalysis
                                  └─ if scream OR boosted score ≥ 0.75 → Alert
                                        ├─ WebSocket /ws/dashboard ─► Central dashboard (PC)
                                        ├─ static /m/              ─► Teacher PWA (phone)
                                        └─ MQTT sams/alerts        ─► other subscribers
                                        (WS push + feed are filtered per user by
                                         staff location assignments — FR16, §9)

 Staff (dashboard / PWA) ──►  POST /api/auth/login → JWT → all /api reads + WS
```

---

## 2. Prerequisites

- **Python 3.12** (the backend `.venv` targets 3.12)
- A free **Groq API key** for speech-to-text — https://console.groq.com (no credit card)
- *(Optional)* **Mosquitto** MQTT broker, only if you want the MQTT fan-out
- A modern browser (Chrome recommended for the PWA install)

---

## 3. Backend setup (do this first)

All commands are run from the `sams_backend` folder. Examples use **PowerShell**
on Windows; the venv Python is `.\.venv\Scripts\python.exe`.

### 3.1 Install dependencies

```powershell
cd sams_backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> First run downloads the multilingual NLP model
> (`textdetox/xlmr-large-toxicity-classifier`, ~1.1 GB) and — on first use of
> speech emotion recognition — the SER model
> (`superb/wav2vec2-base-superb-er`, ~380 MB), caching both locally. The old
> English-only model remains selectable via `NLP_MODEL`. On a 24-phrase
> EN/Malay/Manglish probe set the new model lifted Malay/Manglish toxic
> accuracy from 17% to 83% (English unchanged, zero false positives on benign
> phrases — a small probe, not a benchmark; the keyword booster stays active).
> Speech-to-text uses the **Groq cloud API**, so no large Whisper download is
> needed.

### 3.2 Configure environment

```powershell
copy .env.example .env
```

Open `.env` and set your Groq key and a JWT secret (everything else has
working defaults):

```
GROQ_API_KEY=gsk_your_key_here
JWT_SECRET_KEY=<64 hex chars>     # generate: python -c "import secrets; print(secrets.token_hex(32))"
```

Optional hardening: set `DEVICE_API_KEY=<random string>` to require an
`X-API-Key` header on the ESP32 ingestion endpoints. While it is empty they
stay open (with a startup warning), so the edge device keeps working before
its firmware sends the header.

Leave `MQTT_ENABLED=false` unless you've set up Mosquitto (see §6).

### 3.3 Seed the database

```powershell
.\.venv\Scripts\python.exe utils\seed_db.py
```

Creates 6 school locations (toilet blocks, stairwells, corridors), 6 ESP32
devices, and two demo users. The staff user is assigned to `loc-001` and
`loc-003` (FR16 alert routing — she only receives alerts from those
locations; the admin sees everything):

| Email | Password | Role |
|---|---|---|
| `admin@school.edu.my` | `Admin@1234` | admin |
| `siti@school.edu.my` | `Staff@1234` | staff |

### 3.4 Run the server

For local use on the PC only:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

To also reach it from a **phone on the same Wi-Fi**, bind to all interfaces:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

- API docs: http://localhost:8000/docs
- Health check: http://localhost:8000/health

---

## 4. Central dashboard (disciplinary staff, on the PC)

The central monitoring computer is a single web page.

```powershell
start sams_dashboard\index.html
```

Sign in with one of the seeded accounts (§3.3), e.g. `admin@school.edu.my` /
`Admin@1234`. A green **"Live"** dot (top-right) means the WebSocket is
connected. It shows the live alert feed, incident detail (threat score,
transcript, acoustic dB/Hz/edge confidence, audio playback), analytics, and the
resolve flow — resolving an alert records **who** resolved it. Live alerts also
carry a detected-**emotion** chip (from the SER model, §4.2), and the alert
feed is filtered per user by staff location assignments (§4.3). Use the user
chip in the header to sign out.

> The dashboard talks to `http://localhost:8000`, so keep the backend running.

### 4.1 Reports (FR13)

The analytics section has a **Reports** card. Admins can generate a report for
the last *N* days (`POST /api/reports/generate?days=N`); each report links the
period's events on a first-report-wins basis. Anyone signed in can list
reports, open a summary (totals, severity/status breakdown, top hotspots,
average minutes-to-resolve), and download it as CSV — the export is
formula-injection-safe. Staff see the card without the Generate button.

### 4.2 Speech emotion recognition (SER)

Every clip is also classified by `superb/wav2vec2-base-superb-er`
(angry / happy / neutral / sad, ~380 MB on first use). If **angry** is
detected with ≥ 0.60 confidence, the NLP threat score gets a **+0.15 boost**
(capped at 1.0) *before* the alert decision, and every result is stored in the
`emotion_analyses` table. Tune via `SER_ENABLED` / `SER_MODEL` / `SER_BOOST` /
`SER_MIN_CONFIDENCE` in `.env`. Honest caveat: the 4-class model has no
"fearful" class — screams are already covered by the scream-detection path, so
SER mainly catches angry speech.

### 4.3 Alert routing by location (FR16)

Staff can be assigned to locations (`staff_locations` table). Admins always
see all alerts; staff with assignments see only alerts from their locations
(both the feed and the WebSocket push); staff with **no** assignments see
everything (fail-open by design, so an unassigned account is never blind).
Assignments are managed via the admin-only API in §9.

---

## 5. Teacher phone app (PWA) — setup on your phone

The teacher app is served **by the backend itself** at `/m`. Your phone doesn't
download the project — it just opens a web page over Wi-Fi. The PC must be
running the server, and the phone must be on the **same network**.

### 5.1 Find your PC's LAN IP

```powershell
ipconfig
```

Look under your Wi-Fi adapter for **IPv4 Address** (e.g. `192.168.100.18`).

### 5.2 Run the backend on all interfaces

```powershell
cd sams_backend
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8000
```

### 5.3 Open the firewall (once, needs Administrator)

Right-click **Windows PowerShell → Run as administrator**, then:

```powershell
New-NetFirewallRule -DisplayName "SAMS 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Any
```

> Needed because Windows Firewall blocks incoming connections by default,
> especially on "Public" Wi-Fi profiles.

### 5.4 Open it on the phone

On the phone (same Wi-Fi), open Chrome and go to — using **your** IP from §5.1:

```
http://192.168.100.18:8000/m/
```

Sign in with a seeded account (e.g. `siti@school.edu.my` / `Staff@1234`) and
the live alert feed appears. Fire a test incident (§7) and the phone will
**buzz, beep, and show the alert**.

### 5.5 (Optional) Install it as a real app

Using the app over `http://<LAN-IP>` works fully, but Android only allows a
**full PWA install** (offline/standalone, via a service worker) from a *secure
context* (`https://` or `localhost`). To install for a demo:

- **Chrome flag (easiest):** on the phone go to `chrome://flags` → search
  **"Insecure origins treated as secure"** → add `http://192.168.100.18:8000`
  → set **Enabled** → relaunch Chrome → reopen the URL → menu **⋮ → Install app**.
- **Or HTTPS:** put the backend behind a tunnel (e.g. `cloudflared`, `ngrok`)
  to get an `https://…` URL, then install with no flags.
- **Or on the PC:** open `http://localhost:8000/m/` in desktop Chrome and install
  there (localhost is already a secure context).

---

## 6. MQTT (optional real-time fan-out)

Only needed if you want alerts published to the `sams/alerts` topic for other
subscribers. Install Mosquitto, then set `MQTT_ENABLED=true` in `.env`.

```powershell
# Windows: install from https://mosquitto.org/download/ (runs as a service)
# Watch the bus:
& "C:\Program Files\mosquitto\mosquitto_sub.exe" -h localhost -t "sams/alerts" -v
```

### 6.1 Securing transport (WSS / MQTTS / HTTPS)

**Current posture:** the LAN demo runs plain `ws://` + `http://` + `mqtt://`.
That is acceptable for the FYP demo on a trusted school LAN, but a production
deployment must encrypt all three transports (report §4.6, Security Design).

**HTTPS + WSS (dashboard & PWA).** The easiest path is a TLS-terminating
tunnel in front of the backend on port 8000 — no code or certificate setup:

```powershell
# cloudflared (free, no account needed for quick tunnels) — or ngrok http 8000
cloudflared tunnel --url http://localhost:8000
```

This prints a public `https://…` URL. What happens to the WebSocket then
depends on which frontend you use:

- **Teacher PWA (`sams_mobile/app.js`)** derives its WS URL from the page
  origin (`location.protocol === 'https:' ? 'wss' : 'ws'` + `location.host`),
  so when the page is served over the tunnel's `https://` URL it automatically
  upgrades to `wss://` — no changes needed.
- **Central dashboard (`sams_dashboard/index.html`)** hardcodes
  `const API = 'http://localhost:8000'` and
  `const WS_URL = 'ws://localhost:8000/ws/dashboard'` (lines ~731–732). To use
  it over a tunnel you must edit those two constants to the tunnel's
  `https://…` / `wss://…/ws/dashboard` URLs (or refactor them to derive from
  `location.origin` like the PWA does).

**MQTTS (encrypted MQTT).** Give Mosquitto a TLS listener on port 8883
(certificates from your school CA, an internal CA, or Let's Encrypt):

```conf
# mosquitto.conf — TLS listener
listener 8883
cafile   /etc/mosquitto/certs/ca.crt
certfile /etc/mosquitto/certs/server.crt
keyfile  /etc/mosquitto/certs/server.key
```

Then point the backend at it in `.env`:

```env
MQTT_USE_TLS=true
MQTT_BROKER_PORT=8883
```

The backend's MQTT client calls paho's `tls_set()` when `MQTT_USE_TLS=true`,
which validates the broker's certificate against the system CA store — so use
a broker certificate signed by a CA the server trusts.

**What is / isn't encrypted:**

| Transport                        | LAN demo (default)     | Production (this section)        |
|----------------------------------|------------------------|----------------------------------|
| Dashboard/PWA pages + REST API   | `http://` — plaintext  | `https://` via tunnel/reverse proxy |
| Dashboard/PWA WebSocket alerts   | `ws://` — plaintext    | `wss://` (automatic for the PWA; edit constants for the dashboard) |
| MQTT fan-out (`sams/alerts`)     | `mqtt://1883` — plaintext | `mqtts://8883` (`MQTT_USE_TLS=true`) |
| Backend → Groq / Supabase        | already `https://`     | already `https://`               |

---

## 7. Test the whole pipeline (no hardware needed)

The ingestion endpoint mirrors the real ESP32 flow: the clip must already be
in the Supabase **`audio-clips`** bucket; the endpoint receives only the file
path and downloads it from there.

**Step 1 — upload a test clip to Supabase.** Easiest is the Supabase web
dashboard: *Storage → audio-clips → Upload file* → upload `scream_clip.wav`.

**Step 2 — notify the backend** (with the server running):

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/events/audio `
  -F device_id=esp32-003 -F location_id=loc-003 -F "timestamp=2026-06-17T10:30:00" `
  -F sound_level=115 -F duration_seconds=6 `
  -F supabase_file_path=scream_clip.wav
```

If you set `DEVICE_API_KEY` in `.env`, add: `-H "X-API-Key: <your key>"`.

Expected response: `is_scream`, a real `transcript`, a `threat_score`,
`severity`, and `alert_fired` — plus a toast/beep on both the central
dashboard and the teacher phone app. The first request after startup is slower
while the NLP model loads.

> **Note:** `simulate_edge.py` still targets the old direct-upload contract
> (multipart `audio_file`) and does not work against the current endpoint —
> see §11.

---

## 8. Run the tests

```powershell
cd sams_backend
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

~70 tests: MQTT service, NLP threat classifier, the STT→NLP processing
pipeline (mocked AI, in-memory DB), the JWT auth layer (login, token expiry,
route protection, device API key, resolver stamping), FR16 alert routing
(feed + WebSocket filtering, admin assignment API), FR13 reports
(generation, summaries, CSV export), and SER emotion analysis (boost logic,
settings, storage).

---

## 9. API reference

Interactive docs are always at http://localhost:8000/docs. Key endpoints:

**Auth (staff)**
```
POST /api/auth/login        { "email": "…", "password": "…" }
                            → { "access_token", "token_type", "user" }
GET  /api/auth/me           current user profile (requires token)
```
All staff-facing endpoints below require `Authorization: Bearer <token>`.

**Submit audio (from the ESP32 edge device — clip must already be in Supabase)**
```
POST /api/events/audio          (multipart/form-data)
  device_id          string   "esp32-003"
  location_id        string   "loc-003"
  timestamp          string   "2026-06-17T10:30:00"
  sound_level        string   "115"    (dB from edge)
  duration_seconds   string   "6"
  supabase_file_path string   "clip.wav"  (object key in the audio-clips bucket)
Header (only when DEVICE_API_KEY is set):  X-API-Key: <key>

POST /api/events/webhook/supabase-storage   alternative: Supabase Storage
                                            webhook fires it on upload
                                            (idempotent — skips files already
                                            processed via /audio)
```

**Alerts (used by the dashboard and teacher PWA)**
```
GET  /api/alerts/?status=active&severity=high&per_page=50   feed
GET  /api/alerts/stats                                      counters
GET  /api/alerts/{alert_id}                                 full detail
PUT  /api/alerts/{alert_id}/resolve                         { "resolution_notes": "…" }
                                                            (stamps resolving user)
GET  /api/events/{event_id}/audio?token=<jwt>               clip playback
                                                            (?token= because <audio>
                                                            tags can't send headers)
```

**Analytics**
```
GET  /api/analytics/hotspots             top high-risk locations
GET  /api/analytics/trends?days=14       daily incident trend
GET  /api/analytics/severity-breakdown   counts by severity
```

**Reports (FR13)** — generation is admin-only; list/summary/export for all staff
```
POST /api/reports/generate?days=N        create a report for the last N days
                                         (admin-only → 403 for staff;
                                         first-report-wins event linking)
GET  /api/reports/                       list reports
GET  /api/reports/{id}                   summary: totals, severity/status
                                         breakdown, top hotspots, avg
                                         minutes-to-resolve
GET  /api/reports/{id}/export.csv        CSV download (formula-injection-safe)
```

**Admin — staff location assignments (FR16)** — admin-only (staff get 403)
```
GET  /api/admin/staff                          staff list with assignments
PUT  /api/admin/staff/{user_id}/locations      { "location_ids": ["loc-001", …] }
```

**Real-time**
```
WS   /ws/dashboard?token=<jwt>   live alert push (JSON {type:"ALERT", …});
                                 payload includes the SER result
                                 (emotion + confidence) when available;
                                 alerts are filtered by the user's staff
                                 location assignments (§4.3);
                                 invalid token → close code 4401
MQTT sams/alerts                 same alert payload (when MQTT_ENABLED=true)
```

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| Login returns 503 "Authentication not configured" | `JWT_SECRET_KEY` missing in `.env` (§3.2). |
| API calls return 401 / dashboard bounces to login | Token expired (8 h) or missing — sign in again. |
| Phone can't load `…:8000/m/` | Same Wi-Fi as the PC? Server started with `--host 0.0.0.0`? Firewall rule added (§5.3)? |
| Dashboard dot stuck on "Connecting…" | Backend not running, or opened from a different host than `localhost`. |
| Transcription shows "[transcription unavailable]" | `GROQ_API_KEY` missing or invalid in `.env` (scream alerts still fire). |
| ESP32 gets 401 on `/api/events/audio` | `DEVICE_API_KEY` is set but the device isn't sending the matching `X-API-Key` header. |
| "Install app" option missing on phone | Plain HTTP LAN IP isn't a secure context — see §5.5. |
| MQTT errors at startup | Set `MQTT_ENABLED=false`, or start Mosquitto (§6). |

---

## 11. Known gaps / next steps

- **`simulate_edge.py` is outdated:** it still sends the old direct-upload
  contract (multipart `audio_file`); the active endpoint expects a
  `supabase_file_path` pointing at a clip already in the bucket. Use the §7
  two-step test instead until the simulator is rewritten.
- **CCTV retrieval from nearby authorized areas:** not implemented in this
  repo (descoped).
- **Transport security:** the hardening path (HTTPS/WSS/MQTTS) is documented
  in §6.1, but the LAN demo still runs plain `http://` / `ws://` / `mqtt://`
  by default — encrypting all three needs a TLS-terminating deploy.
- **Edge integration:** real ESP32-C3 + INMP441 device (teammate's module)
  uploads to Supabase then notifies `/api/events/audio` — the backend
  auto-registers devices. When `DEVICE_API_KEY` is enabled, the firmware must
  send the `X-API-Key` header.