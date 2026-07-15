// ── WiFi ────────────────────────────────────────────────────────────────
#define WIFI_SSID     "Connecting_2.4GHz"
#define WIFI_PASSWORD "0122260030"

// ── Backend server (FastAPI) ────────────────────────────────────────────
// LAN IP of the PC running the backend — reconfigure before each upload.
#define SERVER_IP     "192.168.100.18"
#define SERVER_PORT   8000

// ── Supabase Storage ────────────────────────────────────────────────────
// Use the project *anon* key here — with a storage RLS policy that only
// allows INSERT into the audio-clips bucket (see README).
// NEVER put the service_role key on a device.
#define SUPABASE_HOST    "lljkntrbthoycllpeckq.supabase.co"
#define SUPABASE_KEY     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxsamtudHJidGhveWNsbHBlY2txIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MjI2Mzg0MSwiZXhwIjoyMDk3ODM5ODQxfQ.-mOZpwV74iChOt8bOFHr56uATPl0htfJrGRI4-clmOw"
