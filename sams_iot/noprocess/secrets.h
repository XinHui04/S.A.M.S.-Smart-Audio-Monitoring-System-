// ── WiFi ────────────────────────────────────────────────────────────────
#define WIFI_SSID     "OPPO A92"
#define WIFI_PASSWORD "brightstart"

// ── Backend server (FastAPI) ────────────────────────────────────────────
// LAN IP of the PC running the backend — reconfigure before each upload.
#define SERVER_IP     "172.16.40.144"
#define SERVER_PORT   8000

// ── Supabase Storage ────────────────────────────────────────────────────
// Use the project *anon* key here — with a storage RLS policy that only
// allows INSERT into the audio-clips bucket (see README).
// NEVER put the service_role key on a device.
#define SUPABASE_HOST    "lljkntrbthoycllpeckq.supabase.co"
#define SUPABASE_KEY     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxsamtudHJidGhveWNsbHBlY2txIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MjI2Mzg0MSwiZXhwIjoyMDk3ODM5ODQxfQ.-mOZpwV74iChOt8bOFHr56uATPl0htfJrGRI4-clmOw"
