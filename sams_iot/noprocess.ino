// ================================================================
// S.A.M.S. - ESP32 30 pins (Audio Capture Only - No Processing)
// ================================================================

#include <Arduino.h>
#include <ArduinoJson.h>
#include <driver/i2s.h>
#include <driver/adc.h>
#include <WiFi.h> 
#include <WiFiClientSecure.h>
#include "esp_wifi.h" 
#include "esp_bt.h" 

// ── WiFi Configuration ──────────────────────────────────────────────────
const char* wifi_ssid = "";
const char* wifi_password = "";
WiFiClient espClient;
bool ledwifi_state = false; 

// ── Backend server (FastAPI) ─────────────────────────────────────────
#define SERVER_IP        "192.168.0.7"          // needs to reconfigure everytime before upload 
#define SERVER_PORT      8000
#define NOTIFY_ENDPOINT "/api/events/audio"     // backend receives metadata + Supabase path

// ── Supabase Storage (HTTPS) ──────────────────────────────────────────────
#define SUPABASE_HOST    "lljkntrbthoycllpeckq.supabase.co"
#define SUPABASE_BUCKET  "audio-clips"
#define SUPABASE_KEY     "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxsamtudHJidGhveWNsbHBlY2txIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MjI2Mzg0MSwiZXhwIjoyMDk3ODM5ODQxfQ.-mOZpwV74iChOt8bOFHr56uATPl0htfJrGRI4-clmOw"

// ── Device identity ─────────────────────────────────────────────────────────
#define DEVICE_ID        "esp32-001"
#define LOCATION_ID      "loc-toilet-a"

// ── HTTP upload settings ─────────────────────────────────────────────────────
#define HTTP_TIMEOUT_MS  60000
#define BOUNDARY         "----SAMSBoundary7MA4YWxkTrZu0gW"
#define TZ_OFFSET_SEC    (8 * 3600)

// ── WAV Header ──────────────────────────────────────────────────────────────
#define WAV_HEADER_SIZE  44

// Pin Configuration
#define SOUND_SENSOR_PIN  34
#define SOUND_THRESHOLD 3000
#define ALERT_LED_PIN 2
#define I2S_WS_PIN   19
#define I2S_SCK_PIN  21
#define I2S_SD_PIN   18

// Audio Settings
#define SAMPLE_RATE 16000
#define RECORD_SECONDS 8
#define CHUNK_SAMPLES 256

// ════════════════════════════════════════════════════════════════════════════
// Globals
// ════════════════════════════════════════════════════════════════════════════
unsigned long lastTriggerTime = 0;
const unsigned long triggerCooldownMs = 15000;  // for 8s clips
bool isLedAlertActive = false;

// Small stack buffers only — no large heap allocation needed
int32_t  rawSamples[CHUNK_SAMPLES];   // I2S raw 32-bit samples  (~1KB)
int16_t  pcmChunk[CHUNK_SAMPLES];     // converted 16-bit samples (~512B)

// ════════════════════════════════════════════════════════════════════════════
// UUID Generator (matches your bucket format)
// ════════════════════════════════════════════════════════════════════════════
void generateUUID(char* out, size_t len) {
    if (len < 37) return; // Need at least 37 chars for UUID (36 + null)
    
    uint32_t r1 = esp_random();
    uint32_t r2 = esp_random();
    uint32_t r3 = esp_random();
    uint32_t r4 = esp_random();
    
    // Format: 8-4-4-4-12 (standard UUID format)
    // Example: 1ea33c87-35db-40a3-903e-d1e512fe5c4a
    snprintf(out, len, "%08x-%04x-%04x-%04x-%08x%04x",
             r1, 
             (r2 >> 16) & 0xFFFF,
             r2 & 0xFFFF,
             (r3 >> 16) & 0xFFFF,
             r3 & 0xFFFFFFFF,
             r4 & 0xFFFF);
}

// ════════════════════════════════════════════════════════════════════════════
// WAV Header Builder
// ════════════════════════════════════════════════════════════════════════════
void buildWavHeader(uint8_t* header, uint32_t numSamples) {
    uint32_t dataBytes   = numSamples * 2;
    uint32_t fileSize    = dataBytes + WAV_HEADER_SIZE - 8;
    uint32_t sampleRate  = SAMPLE_RATE;
    uint32_t byteRate    = sampleRate * 2;
    uint16_t blockAlign  = 2;
    uint16_t bitsPerSamp = 16;
    uint16_t numChannels = 1;
    uint16_t audioFmt    = 1;
    uint32_t fmtSize     = 16;

    memcpy(header,      "RIFF", 4);
    memcpy(header + 4,  &fileSize,    4);
    memcpy(header + 8,  "WAVE", 4);
    memcpy(header + 12, "fmt ", 4);
    memcpy(header + 16, &fmtSize,     4);
    memcpy(header + 20, &audioFmt,    2);
    memcpy(header + 22, &numChannels, 2);
    memcpy(header + 24, &sampleRate,  4);
    memcpy(header + 28, &byteRate,    4);
    memcpy(header + 32, &blockAlign,  2);
    memcpy(header + 34, &bitsPerSamp, 2);
    memcpy(header + 36, "data", 4);
    memcpy(header + 40, &dataBytes,   4);
}

// ── Function Declarations ──────────────────────────────────────────────────
void setupI2S();
void setupWiFi();
void setupNTP();
void getISO8601Timestamp(char* buf, size_t len);
bool streamRecordToSupabase(const char* filename, int soundLevel,
                            const char* timestamp);
bool notifyBackend(const char* supabasePath, int soundLevel,
                   const char* timestamp);
void triggerSoundDetectedAlert();
void triggerSolidLedAlert();

// ════════════════════════════════════════════════════════════════════════════
// Setup
// ════════════════════════════════════════════════════════════════════════════
void setup() {
    esp_bt_controller_disable();

    Serial.begin(115200);
    delay(1000);
    
    Serial.println();
    Serial.println(F("======================================"));
    Serial.println(F("     S.A.M.S. - ESP32 30-PIN          "));
    Serial.println(F("======================================"));

    WiFi.disconnect(true);
    delay(1000);
    WiFi.mode(WIFI_STA);
    delay(1000);

    esp_wifi_set_max_tx_power(WIFI_POWER_8_5dBm);

    // ADC1_CHANNEL_6 = GPIO34 on standard ESP32 30-pin
    adc1_config_width(ADC_WIDTH_BIT_12);
    adc1_config_channel_atten(ADC1_CHANNEL_6, ADC_ATTEN_DB_11);

    setupWiFi();
    setupNTP();
    setupI2S();

    pinMode(SOUND_SENSOR_PIN, INPUT);
    pinMode(ALERT_LED_PIN, OUTPUT);
    digitalWrite(ALERT_LED_PIN, LOW);

    // // Allocate the audio buffer once at startup
    // // 8s * 16000 = 128,000 samples * 2 bytes = 256 KB

    Serial.printf("Free heap: %u bytes\n", ESP.getFreeHeap());
    Serial.println(F("✅ System ready!"));
    Serial.println(F("📡 Waiting for sound trigger..."));
}

// ════════════════════════════════════════════════════════════════════════════
// Loop
// ════════════════════════════════════════════════════════════════════════════
void loop() {
    // ── WiFi status LED ──────────────────────────────────────────────────
    if (WiFi.status() != WL_CONNECTED) {
        digitalWrite(ALERT_LED_PIN, HIGH); delay(80);
        digitalWrite(ALERT_LED_PIN, LOW);  delay(80);
        digitalWrite(ALERT_LED_PIN, HIGH); delay(80);
        digitalWrite(ALERT_LED_PIN, LOW);
    } else {
        if (!isLedAlertActive) {
            digitalWrite(ALERT_LED_PIN, LOW);
        }
    }

    int soundLevel = adc1_get_raw(ADC1_CHANNEL_6);

    static int counter = 0;
    if (++counter >= 20) {
        counter = 0;
        Serial.print(F("Sound level: "));
        Serial.println(soundLevel);
    }

    bool thresholdExceeded = soundLevel >= SOUND_THRESHOLD;
    bool cooldownPassed = millis() - lastTriggerTime >= triggerCooldownMs;

    if (thresholdExceeded && cooldownPassed) {
        lastTriggerTime = millis();

        Serial.println();
        Serial.print(F("🔊 Sound level "));
        Serial.print(soundLevel);
        Serial.print(F(" exceeded threshold ("));
        Serial.print(SOUND_THRESHOLD);
        Serial.println(F(")"));

        triggerSoundDetectedAlert();

        // ── Get timestamp before recording ────────────────────────────────
        char timestamp[30];
        getISO8601Timestamp(timestamp, sizeof(timestamp));

        // ── Generate UUID filename (matches your bucket format) ──────────
        char uuid[37];
        generateUUID(uuid, sizeof(uuid));
        
        char filename[80];
        snprintf(filename, sizeof(filename), "%s.wav", uuid);
        // Creates: 1ea33c87-35db-40a3-903e-d1e512fe5c4a.wav  ← This matches bucket

        Serial.println(F("🎙️ Recording + streaming 8s to Supabase..."));

        bool ok = streamRecordToSupabase(filename, soundLevel, timestamp);

        if (ok) {
            Serial.println(F("✅ Stream + upload complete!"));
        } else {
            Serial.println(F("❌ Stream/upload failed!"));
        }

        Serial.println();
    }

    delay(50);
}

// ════════════════════════════════════════════════════════════════════════════
// Stream-record: record I2S audio and simultaneously stream it to Supabase
// over HTTPS using chunked transfer encoding.
// No large buffer needed — only CHUNK_SAMPLES * 4 bytes at a time in RAM.
// ════════════════════════════════════════════════════════════════════════════
bool streamRecordToSupabase(const char* filename, int soundLevel,
                            const char* timestamp) {

    WiFiClientSecure client;
    client.setInsecure();
    client.setTimeout(HTTP_TIMEOUT_MS / 1000);

    if (!client.connect(SUPABASE_HOST, 443)) {
        Serial.println(F("[Supabase] HTTPS connect failed"));
        return false;
    }

    uint32_t totalSamples  = (uint32_t)SAMPLE_RATE * RECORD_SECONDS;
    uint32_t pcmBytes      = totalSamples * sizeof(int16_t);
    uint32_t totalWavBytes = WAV_HEADER_SIZE + pcmBytes;

    // Build Supabase REST path
    String path = "/storage/v1/object/";
    path += SUPABASE_BUCKET;
    path += "/";
    path += filename;

    Serial.printf("[Supabase] PUT %s (%u bytes)\n", path.c_str(), totalWavBytes);

    // ── Send HTTP headers ─────────────────────────────────────────────────
    client.print("PUT ");
    client.print(path);
    client.println(" HTTP/1.1");
    client.print("Host: ");
    client.println(SUPABASE_HOST);
    client.print("Authorization: Bearer ");
    client.println(SUPABASE_KEY);
    client.println("Content-Type: audio/wav");
    client.print("Content-Length: ");
    client.println(totalWavBytes);
    client.println("x-upsert: true");       // prevents hang on duplicate filename
    client.println("Connection: close");
    client.println();                       // end of headers

    // ── Send WAV header first ─────────────────────────────────────────────
    uint8_t wavHeader[WAV_HEADER_SIZE];
    buildWavHeader(wavHeader, totalSamples);
    client.write(wavHeader, WAV_HEADER_SIZE);

    // ── Record I2S and stream PCM chunks directly ─────────────────────────
    uint32_t samplesStreamed = 0;

    while (samplesStreamed < totalSamples) {
        size_t   bytesRead = 0;
        uint32_t toRead    = min((uint32_t)CHUNK_SAMPLES,
                                 totalSamples - samplesStreamed);

        esp_err_t result = i2s_read(
            I2S_NUM_0,
            rawSamples,
            toRead * sizeof(int32_t),
            &bytesRead,
            portMAX_DELAY
        );

        if (result != ESP_OK || bytesRead == 0) {
            Serial.println(F("[I2S] Read error during streaming"));
            client.stop();
            return false;
        }

        int n = bytesRead / sizeof(int32_t);

        // Convert 32-bit I2S samples → 16-bit PCM
        for (int i = 0; i < n; i++) {
            pcmChunk[i] = (int16_t)(rawSamples[i] >> 16);
        }

        // Stream directly to Supabase
        client.write((uint8_t*)pcmChunk, n * sizeof(int16_t));
        samplesStreamed += n;

        yield();  // keep WiFi stack alive during long transfer
    }

    Serial.printf("[Supabase] Streamed %u samples (%.1fs)\n",
                  samplesStreamed, (float)samplesStreamed / SAMPLE_RATE);

    // ── Read response ─────────────────────────────────────────────────────
    unsigned long deadline = millis() + 15000;
    while (!client.available() && client.connected() && millis() < deadline) {
        delay(10);
    }

    String statusLine = "";
    bool gotStatus = false;

    while (client.connected() && millis() < deadline) {
        if (client.available()) {
            String line = client.readStringUntil('\n');
            line.trim();

            if (!gotStatus && line.indexOf("HTTP") >= 0) {
                statusLine = line;
                gotStatus  = true;
                Serial.print(F("[Supabase] "));
                Serial.println(statusLine);
                continue;
            }
            if (line.length() == 0) break;
        }
    }
    client.stop();
    delay(500);

    if (!gotStatus) {
        Serial.println(F("[Supabase] No response received"));
        return false;
    }

    bool success = statusLine.indexOf("200") > 0 ||
                   statusLine.indexOf("201") > 0;

    if (success) {
        notifyBackend(filename, soundLevel, timestamp);
    } else {
        Serial.print(F("[Supabase] Upload failed: "));
        Serial.println(statusLine);
    }

    return success;
}

// ════════════════════════════════════════════════════════════════════════════
// Notify FastAPI backend — sends metadata + Supabase file path
// Backend downloads from Supabase, runs scream analysis, saves to DB
// ════════════════════════════════════════════════════════════════════════════
bool notifyBackend(const char* supabasePath, int soundLevel,
                   const char* timestamp) {

    WiFiClient client;

    if (!client.connect(SERVER_IP, SERVER_PORT)) {
        Serial.println(F("[Backend] TCP connect failed"));
        return false;
    }

    client.setTimeout(30);

    String bnd  = BOUNDARY;
    String body = "";

    auto addField = [&](const char* name, String value) {
        body += "--" + bnd + "\r\n";
        body += "Content-Disposition: form-data; name=\"";
        body += name;
        body += "\"\r\n\r\n";
        body += value + "\r\n";
    };

    addField("device_id",          DEVICE_ID);
    addField("location_id",        LOCATION_ID);
    addField("timestamp",          String(timestamp));
    addField("sound_level",        String(soundLevel));
    addField("duration_seconds",   String(RECORD_SECONDS));
    addField("supabase_file_path", String(supabasePath));

    body += "--" + bnd + "--\r\n";

    Serial.printf("[Backend] POST %s\n", NOTIFY_ENDPOINT);

    client.print("POST ");
    client.print(NOTIFY_ENDPOINT);
    client.println(" HTTP/1.1");
    client.print("Host: "); client.print(SERVER_IP);
    client.print(":"); client.println(SERVER_PORT);
    client.print("Content-Type: multipart/form-data; boundary=");
    client.println(BOUNDARY);
    client.print("Content-Length: "); client.println(body.length());
    client.println("Connection: close");
    client.println();
    client.print(body);

    // ── Read full response ───────────────────────────────────────────────
    unsigned long deadline = millis() + 15000;
    String response = "";
    bool headersEnded = false;

    while (client.connected() && millis() < deadline) {
        if (client.available()) {
            String line = client.readStringUntil('\n');
            
            // Check if we've reached the end of headers
            if (line == "\r" || line == "\n" || line == "\r\n" || line.length() <= 1) {
                headersEnded = true;
                continue;
            }
            
            // If headers ended, accumulate the body
            if (headersEnded) {
                response += line;
                // Continue reading the rest of the body
                while (client.available()) {
                    response += client.readString();
                }
                break;
            }
        }
    }
    client.stop();

    // Trim response
    response.trim();
    
    Serial.print("[Backend] Response: ");
    Serial.println(response.substring(0, 200)); // Print first 200 chars

    // ── Parse JSON response ──────────────────────────────────────────────
    if (response.length() == 0) {
        Serial.println(F("[Backend] Empty response"));
        return false;
    }

    // Find where JSON starts (after HTTP headers)
    int jsonStart = response.indexOf('{');
    if (jsonStart < 0) {
        Serial.println(F("[Backend] No JSON found in response"));
        return false;
    }
    
    String jsonStr = response.substring(jsonStart);
    Serial.print("[Backend] JSON: ");
    Serial.println(jsonStr);

    DynamicJsonDocument doc(512);
    DeserializationError error = deserializeJson(doc, jsonStr);
    
    if (error) {
        Serial.print(F("[Backend] JSON parse: "));
        Serial.println(error.c_str());
        return false;
    }

    // Extract values
    bool  isScream   = doc["is_scream"]   | false;
    float confidence = doc["confidence"]  | 0.0f;
    bool  alertFired = doc["alert_fired"] | false;
    String message   = doc["message"]     | "";

    Serial.printf("[Backend] isScream=%d confidence=%.3f alertFired=%d\n",
                  isScream, confidence, alertFired);

    if (isScream) {
        Serial.printf("🔴 SCREAM DETECTED! (%.1f%%)\n", confidence * 100);
        Serial.printf("📝 Message: %s\n", message.c_str());
        if (alertFired) {
            Serial.println(F("🚨 ALERT FIRED!"));
            triggerSolidLedAlert();
        }
    } else {
        Serial.printf("🟢 No scream (%.1f%%)\n", confidence * 100);
        Serial.printf("📝 Message: %s\n", message.c_str());
    }

    return true;
}

// ════════════════════════════════════════════════════════════════════════════
// WiFi Setup
// ════════════════════════════════════════════════════════════════════════════
void setupWiFi() {
    delay(10);
    WiFi.begin(wifi_ssid, wifi_password);

    int attempts = 0;
    while (WiFi.status() != WL_CONNECTED && attempts < 40) {
        Serial.print(".");
        delay(500);
        attempts++;
    }
    Serial.println();

    if (WiFi.status() == WL_CONNECTED) {
        Serial.println(F("✅ WiFi connected!"));
        Serial.print("   IP: ");
        Serial.println(WiFi.localIP());
        Serial.print(F("   RSSI: "));
        Serial.print(WiFi.RSSI());
        Serial.println(F(" dBm"));
    } else {
        Serial.println(F("❌ WiFi connection FAILED!"));
    }
}

// ════════════════════════════════════════════════════════════════════════════
// NTP Setup
// ════════════════════════════════════════════════════════════════════════════
void setupNTP() {
    configTime(TZ_OFFSET_SEC, 0, "pool.ntp.org", "time.google.com");
    Serial.print("[NTP] Syncing time");
    struct tm timeinfo;
    uint8_t retries = 0;
    while (!getLocalTime(&timeinfo) && retries < 15) {
        delay(500);
        Serial.print(".");
        retries++;
    }
    if (retries < 15) {
        char buf[30];
        strftime(buf, sizeof(buf), "%Y-%m-%dT%H:%M:%S", &timeinfo);
        Serial.print(" OK → ");
        Serial.println(buf);
    } else {
        Serial.println(F(" FAILED (using fallback)"));
    }
}

void getISO8601Timestamp(char* buf, size_t len) {
    struct tm timeinfo;
    if (getLocalTime(&timeinfo)) {
        strftime(buf, len, "%Y-%m-%dT%H:%M:%S", &timeinfo);
    } else {
        snprintf(buf, len, "2026-06-23T00:00:00");
    }
}

// ════════════════════════════════════════════════════════════════════════════
// I2S Setup (ESP32 30-pin)
// ════════════════════════════════════════════════════════════════════════════
void setupI2S() {
    Serial.println(F("Initializing I2S..."));

    i2s_config_t i2s_config = {
        .mode              = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX),
        .sample_rate       = SAMPLE_RATE,
        .bits_per_sample   = I2S_BITS_PER_SAMPLE_32BIT,
        .channel_format    = I2S_CHANNEL_FMT_ONLY_LEFT,
        .communication_format = I2S_COMM_FORMAT_STAND_I2S,
        .intr_alloc_flags  = ESP_INTR_FLAG_LEVEL1,
        .dma_buf_count     = 4,    // CHANGED: 2→4 for dual-core ESP32 stability
        .dma_buf_len       = 256,
        .use_apll          = false,
        .tx_desc_auto_clear = false,
        .fixed_mclk        = 0
    };

    // CHANGED: added mck_io_num required by standard ESP32 IDF
    i2s_pin_config_t pin_config = {
        .mck_io_num   = I2S_PIN_NO_CHANGE,
        .bck_io_num   = I2S_SCK_PIN,
        .ws_io_num    = I2S_WS_PIN,
        .data_out_num = I2S_PIN_NO_CHANGE,
        .data_in_num  = I2S_SD_PIN
    };

    esp_err_t err = i2s_driver_install(I2S_NUM_0, &i2s_config, 0, NULL);
    if (err != ESP_OK) {
        Serial.printf("I2S install failed: %d\n", err);
        return;
    }

    err = i2s_set_pin(I2S_NUM_0, &pin_config);
    if (err != ESP_OK) {
        Serial.printf("I2S pin set failed: %d\n", err);
        return;
    }

    i2s_zero_dma_buffer(I2S_NUM_0);
    Serial.println(F("I2S ready."));
}

// ════════════════════════════════════════════════════════════════════════════
// LED Alerts
// ════════════════════════════════════════════════════════════════════════════
void triggerSoundDetectedAlert() {
    Serial.println(F("🔊 Sound detected - LED blinking"));
    for (int i = 0; i < 2; i++) {
        digitalWrite(ALERT_LED_PIN, HIGH);
        delay(150);
        digitalWrite(ALERT_LED_PIN, LOW);
        delay(150);
    }
}

void triggerSolidLedAlert() {
    isLedAlertActive = true;
    Serial.println(F("🔴 LED alert ON for 3 seconds"));
    digitalWrite(ALERT_LED_PIN, HIGH);
    delay(3000);
    digitalWrite(ALERT_LED_PIN, LOW);
    isLedAlertActive = false;
}