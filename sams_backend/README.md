# S.A.M.S. Cloud Backend

**Smart Audio Monitoring System** — Cloud Processing, AI Analysis & Monitoring API  
FYP Project by Lim Xin Hui | TARUMT 2025/26

---

## 🏗️ Architecture Overview

```
ESP32-C3 Edge Device
    │  (WAV audio + metadata via HTTP multipart)
    ▼
POST /api/events/audio
    │
    ├─ 1. Save Event + AudioClip to DB
    ├─ 2. Whisper STT → Transcript
    ├─ 3. XLM-RoBERTa NLP → ThreatResult
    ├─ 4. Persist Analysis to DB
    └─ 5. IF score ≥ threshold → Alert + MQTT publish
                                        │
                                        ▼
                              sams/alerts MQTT topic
                                        │
                                        ▼
                              Web Dashboard (real-time)
```

---

## 📁 File Structure

```
sams_backend/
├── main.py                         # FastAPI app entry point
├── requirements.txt
├── .env.example                    # Copy to .env and configure
├── api/
│   ├── dependencies.py             # Dependency injection (DB, pipeline)
│   ├── events.py                   # POST /api/events/audio
│   ├── alerts.py                   # GET/PUT /api/alerts/
│   ├── analytics.py                # GET /api/analytics/
│   └── auth.py                     # POST /api/auth/login
├── services/
│   ├── stt_service.py              # Whisper speech-to-text
│   ├── nlp_service.py              # XLM-RoBERTa threat classifier
│   ├── mqtt_service.py             # MQTT publisher
│   └── processing_pipeline.py     # Main orchestrator
├── models/
│   ├── database.py                 # SQLAlchemy ORM models (ERD)
│   └── schemas.py                  # Pydantic request/response schemas
├── utils/
│   ├── auth.py                     # JWT helpers
│   └── seed_db.py                  # Seed script for dev data
├── config/
│   └── settings.py                 # Env config with pydantic-settings
└── tests/
    └── test_nlp_service.py         # NLP unit tests
```

---

## 🚀 Setup & Run

### 1. Install dependencies

```bash
cd sams_backend
pip install -r requirements.txt
```

> **Note:** First run downloads the Whisper model (~140MB for `base`) and
> XLM-RoBERTa model (~300MB). These are cached locally after that.

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set MQTT_BROKER_HOST and SECRET_KEY
```

### 3. Seed the database

```bash
python utils/seed_db.py
```

This creates:
- 6 school locations (toilet blocks, stairwells, corridors)
- 6 ESP32 devices mapped to those locations
- Admin user: `admin@school.edu.my` / `Admin@1234`

### 4. Run the server

```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```

**API Docs:** http://localhost:8000/docs

---

## 🔌 MQTT Setup (Local Dev)

Install Mosquitto broker:

```bash
# macOS
brew install mosquitto && brew services start mosquitto

# Ubuntu/Debian
sudo apt install mosquitto mosquitto-clients
sudo systemctl start mosquitto

# Windows — download from https://mosquitto.org/download/
```

Test it works:
```bash
mosquitto_sub -t "sams/alerts" -v &   # listen for alerts
mosquitto_pub -t "sams/test" -m "hello"
```

---

## 📡 API Reference

### Submit Audio (from ESP32)
```
POST /api/events/audio
Content-Type: multipart/form-data

Fields:
  device_id        string   "esp32-001"
  location_id      string   "loc-001"
  timestamp        string   "2026-06-11T10:30:00"
  intensity        float    85.3   (dB)
  pitch            float    320.0  (Hz)
  confidence_score float    0.82   (edge scream score)
  duration_seconds float    5.0
  audio_file       file     audio.wav
```

### Get Alerts
```
GET /api/alerts/?status=active&severity=high
```

### Resolve Alert
```
PUT /api/alerts/{alert_id}/resolve
{ "resolution_notes": "Investigated. False alarm from PE class." }
```

### Analytics
```
GET /api/analytics/hotspots     # top risky locations
GET /api/analytics/trends       # daily incident trend
GET /api/analytics/recent       # latest high-severity incidents
```

---

## 🧪 Testing

```bash
pytest tests/ -v
```

---

## ⚙️ Configuration Reference

| Variable | Default | Description |
|---|---|---|
| `STT_PROVIDER` | `whisper_local` | `whisper_local` or `openai_api` |
| `WHISPER_MODEL_SIZE` | `base` | `tiny/base/small/medium` — larger = more accurate |
| `NLP_MODEL` | `cardiffnlp/twitter-xlm-roberta-base-offensive` | Multilingual threat classifier |
| `THREAT_SCORE_THRESHOLD` | `0.75` | Alert threshold (0–1) |
| `MQTT_BROKER_HOST` | `localhost` | Your MQTT broker address |

---

## 🔜 Next Module: Dashboard Frontend

The dashboard frontend (Next.js / React) connects to:
- `GET /api/alerts/` for the alert feed
- `GET /api/analytics/` for charts
- MQTT `sams/alerts` topic for real-time push notifications
