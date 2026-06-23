# S.A.M.S. — Smart Audio Monitoring System

Privacy-aware bullying detection for school zones where cameras can't go.
An edge device sends a short audio clip to the cloud backend, which transcribes
it, runs an NLP threat classifier, and — if the threat score crosses the
threshold — raises an alert that is pushed live to a **central web dashboard**
(disciplinary staff) and a **teacher phone app** (PWA).

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
│   ├── simulate_edge.py         Stand-in for the ESP32 device (testing)
│   ├── api/                     events.py · alerts.py · analytics.py · dependencies.py
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
├── PROGRESS.md                  Milestone progress report
└── README.md                    You are here
```

The three pieces talk over one backend:

```
 Edge device (ESP32-C3)          sams_backend  (FastAPI, port 8000)
   scream + audio  ──HTTP──►  POST /api/events/audio
                                  │  VAD → Whisper STT → NLP threat score
                                  │  store to SQLite
                                  └─ if score ≥ 0.75 → Alert
                                        ├─ WebSocket /ws/dashboard ─► Central dashboard (PC)
                                        ├─ static /m/             ─► Teacher PWA (phone)
                                        └─ MQTT sams/alerts        ─► other subscribers
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

Open `.env` and set your Groq key (everything else has working defaults):

```
GROQ_API_KEY=gsk_your_key_here
```

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

A green **"Live"** dot (top-right) means the WebSocket is connected. It shows the
live alert feed, incident detail (threat score, transcript, acoustic dB/Hz/edge
confidence, audio playback), analytics, and the resolve flow.

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

Sign in (any name → pick a role → **Enter**) and the live alert feed appears.
Fire a test incident (§7) and the phone will **buzz, beep, and show the alert**.

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

With the backend running, simulate the edge device from `sams_backend`:

```powershell
.\.venv\Scripts\python.exe simulate_edge.py --clip scream_clip.wav --device esp32-003 --location loc-003 --intensity 115 --pitch 2400 --confidence 0.97
```

Or with curl:

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/events/audio `
  -F device_id=esp32-003 -F location_id=loc-003 -F "timestamp=2026-06-17T10:30:00" `
  -F intensity=115 -F pitch=2400 -F confidence_score=0.97 -F duration_seconds=6 `
  -F "audio_file=@scream_clip.wav;type=audio/wav"
```

Expected: a transcript, a high threat score, an alert fired — and a toast/beep on
both the central dashboard and the teacher phone app.

---

## 8. Run the tests

```powershell
cd sams_backend
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

---

## 9. API reference

Interactive docs are always at http://localhost:8000/docs. Key endpoints:

**Submit audio (from the ESP32 edge device / simulator)**
```
POST /api/events/audio          (multipart/form-data)
  device_id        string   "esp32-003"
  location_id      string   "loc-003"
  timestamp        string   "2026-06-17T10:30:00"
  intensity        float    115.0   (dB)
  pitch            float    2400.0  (Hz)
  confidence_score float    0.97    (edge scream score, 0–1)
  duration_seconds float    6.0
  audio_file       file     clip.wav
```

**Alerts (used by the dashboard and teacher PWA)**
```
GET  /api/alerts/?status=active&severity=high&per_page=50   feed
GET  /api/alerts/stats                                      counters
GET  /api/alerts/{alert_id}                                 full detail
PUT  /api/alerts/{alert_id}/resolve                         { "resolution_notes": "…" }
```

**Analytics**
```
GET  /api/analytics/hotspots             top high-risk locations
GET  /api/analytics/trends?days=14       daily incident trend
GET  /api/analytics/severity-breakdown   counts by severity
```

**Real-time**
```
WS   /ws/dashboard          live alert push (JSON {type:"ALERT", …})
MQTT sams/alerts            same alert payload (when MQTT_ENABLED=true)
```

---

## 10. Troubleshooting

| Symptom | Fix |
|---|---|
| Phone can't load `…:8000/m/` | Same Wi-Fi as the PC? Server started with `--host 0.0.0.0`? Firewall rule added (§5.3)? |
| Dashboard dot stuck on "Connecting…" | Backend not running, or opened from a different host than `localhost`. |
| Transcription empty / fails | `GROQ_API_KEY` missing or invalid in `.env`. |
| "Install app" option missing on phone | Plain HTTP LAN IP isn't a secure context — see §5.5. |
| MQTT errors at startup | Set `MQTT_ENABLED=false`, or start Mosquitto (§6). |

---

## 11. Known gaps / next steps

- **Authentication:** the teacher PWA login is a UI shell (no real auth yet);
  JWT login + role-based access (FR23) is the next backend iteration.
- **Multilingual NLP:** the active model is English-only; Malay/Manglish currently
  relies on a keyword booster. Multilingual XLM-R model swap is planned.
- **Edge integration:** real ESP32-C3 + INMP441 device (teammate's module) POSTs
  to `/api/events/audio` — the backend already auto-registers devices.

See [PROGRESS.md](PROGRESS.md) for the full milestone report.