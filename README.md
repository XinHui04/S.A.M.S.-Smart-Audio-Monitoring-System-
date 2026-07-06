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
│   ├── api/                     auth.py (JWT login) · events.py · alerts.py · analytics.py · dependencies.py
│   ├── services/                stt · nlp · mqtt · audio_capture · processing_pipeline · websocket_manager · storage
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
                                  │  store Event/AudioClip/Transcript/Analysis
                                  └─ if scream OR score ≥ 0.75 → Alert
                                        ├─ WebSocket /ws/dashboard ─► Central dashboard (PC)
                                        ├─ static /m/              ─► Teacher PWA (phone)
                                        └─ MQTT sams/alerts        ─► other subscribers

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

> First run downloads the NLP model (`cardiffnlp/twitter-roberta-base-offensive`,
> ~500 MB) and caches it locally. Speech-to-text uses the **Groq cloud API**, so
> no large Whisper download is needed.

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
devices, and two demo users:

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
resolve flow — resolving an alert records **who** resolved it. Use the user
chip in the header to sign out.

> The dashboard talks to `http://localhost:8000`, so keep the backend running.

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

28 tests: MQTT service, NLP threat classifier, the STT→NLP processing
pipeline (mocked AI, in-memory DB), and the JWT auth layer (login, token
expiry, route protection, device API key, resolver stamping).

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

**Real-time**
```
WS   /ws/dashboard?token=<jwt>   live alert push (JSON {type:"ALERT", …});
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
- **Acknowledge alerts (FR17):** the dashboard has resolve only; a separate
  acknowledge step (per the use case diagram) is not implemented yet.
- **Alert routing by role/proximity (FR16):** all signed-in staff receive all
  alerts; per-location routing is future work.
- **Multilingual NLP:** the active model is English-only; Malay/Manglish currently
  relies on a keyword booster. Multilingual XLM-R model swap is planned.
- **Speech Emotion Recognition (§2.1.3 of the report)** and **CCTV retrieval
  from nearby authorized areas** are not implemented in this repo.
- **Transport security:** WebSocket/MQTT run unencrypted on the LAN demo;
  WSS/MQTTS per the report's Security Design needs a TLS-terminating deploy.
- **Edge integration:** real ESP32-C3 + INMP441 device (teammate's module)
  uploads to Supabase then notifies `/api/events/audio` — the backend
  auto-registers devices. When `DEVICE_API_KEY` is enabled, the firmware must
  send the `X-API-Key` header.